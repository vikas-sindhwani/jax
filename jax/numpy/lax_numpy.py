# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Implements the NumPy API, using the primitives in :mod:`jax.lax`.

NumPy operations are implemented in Python in terms of the primitive operations
in :mod:`jax.lax`. Since NumPy operations are not primitive and instead are
implemented in terms of :mod:`jax.lax` operations, we do not need to define
transformation rules such as gradient or batching rules. Instead,
transformations for NumPy primitives can be derived from the transformation
rules for the underlying :code:`lax` primitives.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from distutils.util import strtobool
import collections
import itertools
import os
import re
import string
import types
import warnings

import numpy as onp
import opt_einsum
import six
from six.moves import builtins, xrange

from jax import jit, device_put, custom_transforms, defjvp
from .. import core
from ..abstract_arrays import UnshapedArray, ShapedArray, ConcreteArray
from ..config import flags
from ..interpreters.xla import DeviceArray
from .. import lax
from ..util import partial, get_module_functions, unzip2, prod as _prod
from ..lib import pytree
from ..lib import xla_bridge
from ..lib import xla_client

FLAGS = flags.FLAGS
flags.DEFINE_enum(
    'jax_numpy_rank_promotion', os.getenv('JAX_NUMPY_RANK_PROMOTION', 'allow'),
    enum_values=['allow', 'warn', 'raise'],
    help=
    'Control NumPy-style automatic rank promotion broadcasting '
    '("allow", "warn", or "raise").')

if six.PY3:
  def removechars(s, chars):
    return s.translate(str.maketrans(dict.fromkeys(chars)))
else:
  def removechars(s, chars):
    return s.translate(None, ''.join(chars))

newaxis = None

# We replace some builtin names to follow Numpy's API, so we capture here.
_abs = builtins.abs
_all = builtins.all
_any = builtins.any
_max = builtins.max
_min = builtins.min
_sum = builtins.sum

# We need some numpy scalars
pi = onp.pi
e = onp.e
inf = onp.inf
NINF = onp.NINF
nan = onp.nan

# And some numpy utility functions
set_printoptions = onp.set_printoptions

# We want isinstance(x, np.ndarray) checks in user code to work with the our
# array-like types, including DeviceArray and UnshapedArray (i.e. the abstract
# array base class). We can override the isinstance behavior directly, without
# having the complexity of multiple inheritance on those classes, by defining
# the ndarray class to have a metaclass with special __instancecheck__ behavior.
_arraylike_types = (onp.ndarray, UnshapedArray, DeviceArray)

class _ArrayMeta(type(onp.ndarray)):
  """Metaclass for overriding ndarray isinstance checks."""

  def __instancecheck__(self, instance):
    try:
      return isinstance(instance.aval, _arraylike_types)
    except AttributeError:
      return isinstance(instance, _arraylike_types)

# pylint: disable=invalid-name
class ndarray(six.with_metaclass(_ArrayMeta, onp.ndarray)):
  def __init__(shape, dtype=None, buffer=None, offset=0, strides=None,
               order=None):
    raise TypeError("jax.numpy.ndarray() should not be instantiated explicitly."
                    " Use jax.numpy.array, or jax.numpy.zeros instead.")
# pylint: enable=invalid-name


isscalar = onp.isscalar
iscomplexobj = onp.iscomplexobj
result_type = onp.result_type
shape = _shape = onp.shape
ndim = _ndim = onp.ndim
size = onp.size
_dtype = lax.dtype

bool_ = onp.bool_
uint8 = onp.uint8
uint16 = onp.uint16
uint32 = onp.uint32
uint64 = onp.uint64
int8 = onp.int8
int16 = onp.int16
int32 = onp.int32
int64 = onp.int64
float16 = onp.float16
float32 = single = onp.float32
float64 = double = onp.float64
complex64 = csingle = onp.complex64
complex128 = cdouble = onp.complex128

flexible = onp.flexible
character = onp.character
object_ = onp.object_
number = onp.number
inexact = onp.inexact
complexfloating = onp.complexfloating
floating = onp.floating
integer = onp.integer
signedinteger = onp.signedinteger
unsignedinteger = onp.unsignedinteger

iinfo = onp.iinfo
finfo = onp.finfo

issubdtype = onp.issubdtype
issubsctype = onp.issubsctype

ComplexWarning = onp.ComplexWarning

array_str = onp.array_str
array_repr = onp.array_repr

save = onp.save
savez = onp.savez
load = onp.load


### utility functions

def _promote_shapes(fun_name, *args):
  """Prepend implicit leading singleton dimensions for Numpy broadcasting."""
  if len(args) < 2:
    return args
  else:
    shapes = [shape(arg) for arg in args]
    nonscalar_ranks = [len(shp) for shp in shapes if shp]
    if not nonscalar_ranks or len(set(nonscalar_ranks)) == 1:
      return args
    else:
      if FLAGS.jax_numpy_rank_promotion != "allow":
        _rank_promotion_warning_or_error(fun_name, shapes)
      result_rank = len(lax.broadcast_shapes(*shapes))
      return [lax.reshape(arg, (1,) * (result_rank - len(shp)) + shp)
              if shp and len(shp) != result_rank else arg
              for arg, shp in zip(args, shapes)]

def _rank_promotion_warning_or_error(fun_name, shapes):
  if FLAGS.jax_numpy_rank_promotion == "warn":
    msg = ("Following NumPy automatic rank promotion for {} on shapes {}. "
           "Set the jax_numpy_rank_promotion config option to 'allow' to "
           "disable this warning; for more information, see "
           "https://jax.readthedocs.io/en/latest/rank_promotion_warning.html.")
    warnings.warn(msg.format(fun_name, ' '.join(map(str, shapes))))
  elif FLAGS.jax_numpy_rank_promotion == "raise":
    msg = ("Operands could not be broadcast together for {} on shapes {} "
           "and with the config option jax_numpy_rank_promotion='raise'. "
           "For more information, see "
           "https://jax.readthedocs.io/en/latest/rank_promotion_warning.html.")
    raise ValueError(msg.format(fun_name, ' '.join(map(str, shapes))))

def _promote_dtypes(*args):
  """Convenience function to apply Numpy argument dtype promotion."""
  # TODO(dougalm,mattjj): This is a performance bottleneck. Consider memoizing.
  if len(args) < 2:
    return args
  else:
    from_dtypes = map(_dtype, args)
    to_dtype = xla_bridge.canonicalize_dtype(result_type(*from_dtypes))
    return [lax.convert_element_type(x, to_dtype)
            if _dtype(x) != to_dtype else x for x in args]

def _promote_to_result_dtype(op, *args):
  """Convenience function to promote args directly to the op's result dtype."""
  to_dtype = _result_dtype(op, *args)
  return [lax.convert_element_type(arg, to_dtype) for arg in args]


def _result_dtype(op, *args):
  """Compute result dtype of applying op to arguments with given dtypes."""
  args = [onp.ones((0,) * ndim(arg), _dtype(arg)) for arg in args]
  return _dtype(op(*args))


def _check_arraylike(fun_name, *args):
  """Check if all args fit JAX's definition of arraylike (ndarray or scalar)."""
  not_array = lambda x: not isinstance(x, ndarray) and not onp.isscalar(x)
  if _any(not_array(arg) for arg in args):
    pos, arg = next((i, arg) for i, arg in enumerate(args) if not_array(arg))
    msg = "{} requires ndarray or scalar arguments, got {} at position {}."
    raise TypeError(msg.format(fun_name, type(arg), pos))


def _promote_args(fun_name, *args):
  """Convenience function to apply Numpy argument shape and dtype promotion."""
  _check_arraylike(fun_name, *args)
  return _promote_shapes(fun_name, *_promote_dtypes(*args))


def _promote_args_like(op, *args):
  """Convenience function to apply shape and dtype promotion to result type."""
  _check_arraylike(op.__name__, *args)
  return _promote_shapes(op.__name__, *_promote_to_result_dtype(op, *args))


def _constant_like(x, const):
  return onp.array(const, dtype=_dtype(x))

_numpy_signature_re = re.compile(r'^([\w., ]+=)?\s*[\w\.]+\(.*\)$')

def _wraps(fun):
  """Like functools.wraps but works with numpy.ufuncs."""
  def wrap(op):
    try:
      # Numpy doc comments have the form:
      # fn(x, y, z)          (optional)
      #
      # A one-line summary
      #
      # ... everything else ...
      # We (a) move the summary to the top, since it is what the Sphinx
      # autosummary extension expects, and (b) add a comment below the summary
      # to the effect that this is a LAX wrapper of a Numpy function.
      sections = fun.__doc__.split("\n\n")

      signatures = []
      summary = None
      for i in xrange(len(sections)):
        if _numpy_signature_re.match(sections[i]):
          signatures.append(sections[i])
        else:
          summary = sections[i].strip()
          break
      body = "\n\n".join(signatures + sections[i + 1:])
      docstr = (
        "{summary}\n\nLAX-backend implementation of :func:`{fun}`. "
        "Original docstring below.\n\n{body}".format(
          summary=summary, fun=fun.__name__, body=body))
      op.__name__ = fun.__name__
      op.__doc__ = docstr
    finally:
      return op
  return wrap

def _canonicalize_axis(axis, num_dims):
  """Canonicalize an axis in (-num_dims, num_dims) to [0, num_dims)."""
  axis = int(axis)
  if axis < 0:
    axis = axis + num_dims
  if axis < 0 or axis >= num_dims:
      raise ValueError(
          "axis {} is out of bounds for array of dimension {}".format(
              axis, num_dims))
  return axis

### implementations of numpy functions in terms of lax


def _one_to_one_unop(numpy_fn, lax_fn, promote_like=False):
  if promote_like:
    fn = lambda x: lax_fn(lax.convert_element_type(x, _result_dtype(numpy_fn, x)))
  else:
    fn = lambda x: lax_fn(x)
  return _wraps(numpy_fn)(fn)

def _one_to_one_binop(numpy_fn, lax_fn, promote_like=False):
  if promote_like:
    fn = lambda x, y: lax_fn(*_promote_args_like(numpy_fn, x, y))
  else:
    fn = lambda x, y: lax_fn(*_promote_args(numpy_fn.__name__, x, y))
  return _wraps(numpy_fn)(fn)

absolute = abs = _one_to_one_unop(onp.absolute, lax.abs)
fabs = _one_to_one_unop(onp.fabs, lax.abs, True)
bitwise_not = _one_to_one_unop(onp.bitwise_not, lax.bitwise_not)
negative = _one_to_one_unop(onp.negative, lax.neg)
positive = _one_to_one_unop(onp.positive, lambda x: x)
sign = _one_to_one_unop(onp.sign, lax.sign)

floor = _one_to_one_unop(onp.floor, lax.floor, True)
ceil = _one_to_one_unop(onp.ceil, lax.ceil, True)
exp = _one_to_one_unop(onp.exp, lax.exp, True)
log = _one_to_one_unop(onp.log, lax.log, True)
expm1 = _one_to_one_unop(onp.expm1, lax.expm1, True)
log1p = _one_to_one_unop(onp.log1p, lax.log1p, True)
sin = _one_to_one_unop(onp.sin, lax.sin, True)
cos = _one_to_one_unop(onp.cos, lax.cos, True)
tan = _one_to_one_unop(onp.tan, lax.tan, True)
arcsin = _one_to_one_unop(onp.arcsin, lax.asin, True)
arccos = _one_to_one_unop(onp.arccos, lax.acos, True)
arctan = _one_to_one_unop(onp.arctan, lax.atan, True)
sinh = _one_to_one_unop(onp.sinh, lax.sinh, True)
cosh = _one_to_one_unop(onp.cosh, lax.cosh, True)
tanh = _one_to_one_unop(onp.tanh, lax.tanh, True)
sqrt = _one_to_one_unop(onp.sqrt, lax.sqrt, True)


add = _one_to_one_binop(onp.add, lax.add)
bitwise_and = _one_to_one_binop(onp.bitwise_and, lax.bitwise_and)
bitwise_or = _one_to_one_binop(onp.bitwise_or, lax.bitwise_or)
bitwise_xor = _one_to_one_binop(onp.bitwise_xor, lax.bitwise_xor)
right_shift = _one_to_one_binop(onp.right_shift, lax.shift_right_arithmetic)
left_shift = _one_to_one_binop(onp.left_shift, lax.shift_left)
equal = _one_to_one_binop(onp.equal, lax.eq)
multiply = _one_to_one_binop(onp.multiply, lax.mul)
not_equal = _one_to_one_binop(onp.not_equal, lax.ne)
subtract = _one_to_one_binop(onp.subtract, lax.sub)
arctan2 = _one_to_one_binop(onp.arctan2, lax.atan2, True)
minimum = _one_to_one_binop(onp.minimum, lax.min)
maximum = _one_to_one_binop(onp.maximum, lax.max)
float_power = _one_to_one_binop(onp.float_power, lax.pow, True)


def _comparison_op(numpy_fn, lax_fn):
  def fn(x, y):
    x, y =  _promote_args(numpy_fn.__name__, x, y)
    # Comparison on complex types are defined as a lexicographic ordering on
    # the (real, imag) pair.
    if issubdtype(_dtype(x), complexfloating):
      rx = lax.real(x)
      ry = lax.real(y)
      return lax.select(lax.eq(rx, ry), lax_fn(lax.imag(x), lax.imag(y)),
                        lax_fn(rx, ry))
    return lax_fn(x, y)
  return _wraps(numpy_fn)(fn)

greater_equal = _comparison_op(onp.greater_equal, lax.ge)
greater = _comparison_op(onp.greater, lax.gt)
less_equal = _comparison_op(onp.less_equal, lax.le)
less = _comparison_op(onp.less, lax.lt)


def _logical_op(np_op, bitwise_op):
  @_wraps(np_op)
  def op(*args):
    zero = lambda x: lax.full_like(x, shape=(), fill_value=0)
    args = (x if onp.issubdtype(_dtype(x), onp.bool_) else lax.ne(x, zero(x))
            for x in args)
    return bitwise_op(*_promote_args(np_op.__name__, *args))
  return op

logical_and = _logical_op(onp.logical_and, lax.bitwise_and)
logical_not = _logical_op(onp.logical_not, lax.bitwise_not)
logical_or = _logical_op(onp.logical_or, lax.bitwise_or)
logical_xor = _logical_op(onp.logical_xor, lax.bitwise_xor)


@_wraps(onp.true_divide)
def true_divide(x1, x2):
  result_dtype = _result_dtype(onp.true_divide, x1, x2)
  x1, x2 = _promote_shapes("true_divide", x1, x2)
  return lax.div(lax.convert_element_type(x1, result_dtype),
                 lax.convert_element_type(x2, result_dtype))


@_wraps(onp.divide)
def divide(x1, x2):
  # decide whether to perform integer division based on Numpy result dtype, as a
  # way to check whether Python 3 style division is active in Numpy
  result_dtype = _result_dtype(onp.divide, x1, x2)
  if onp.issubdtype(result_dtype, onp.integer):
    return floor_divide(x1, x2)
  else:
    return true_divide(x1, x2)


@_wraps(onp.floor_divide)
def floor_divide(x1, x2):
  x1, x2 = _promote_args("floor_divide", x1, x2)
  dtype = _dtype(x1)
  if issubdtype(dtype, integer):
    quotient = lax.div(x1, x2)
    select = logical_and(lax.sign(x1) != lax.sign(x2), lax.rem(x1, x2) != 0)
    # TODO(mattjj): investigate why subtracting a scalar was causing promotion
    return where(select, quotient - onp.array(1, _dtype(quotient)), quotient)
  elif issubdtype(dtype, complexfloating):
    x1r = lax.real(x1)
    x1i = lax.imag(x1)
    x2r = lax.real(x2)
    x2i = lax.imag(x2)
    which = lax.ge(lax.abs(x2r), lax.abs(x2i))
    rat1 = where(which, lax._const(x2i, 1), lax.div(x2r, x2i))
    rat2 = where(which, lax.div(x2i, x2r), lax._const(x2i, 1))
    out = lax.floor(lax.div(lax.add(lax.mul(x1r, rat1), lax.mul(x1i, rat2)),
                            lax.add(lax.mul(x2r, rat1), lax.mul(x2i, rat2))))
    return lax.convert_element_type(out, dtype)
  else:
    return _float_divmod(x1, x2)[0]


@_wraps(onp.divmod)
def divmod(x1, x2):
  x1, x2 = _promote_args("divmod", x1, x2)
  if onp.issubdtype(_dtype(x1), onp.integer):
    return floor_divide(x1, x2), remainder(x1, x2)
  else:
    return _float_divmod(x1, x2)


def _float_divmod(x1, x2):
  # see float_divmod in floatobject.c of CPython
  mod = lax.rem(x1, x2)
  div = lax.div(lax.sub(x1, mod), x2)

  ind = lax.bitwise_and(mod != 0, lax.sign(x2) != lax.sign(mod))
  mod = lax.select(ind, mod + x1, mod)
  div = lax.select(ind, div - _constant_like(div, 1), div)

  return lax.round(div), mod


@_wraps(onp.power)
def power(x1, x2):
  x1 = asarray(x1)
  x2 = asarray(x2)
  x1, x2 = _promote_args_like(onp.power, x1, x2)
  dtype = _dtype(x1)
  if not issubdtype(dtype, integer):
    return lax.pow(x1, x2)

  # Integer power => use binary exponentiation.

  # TODO(phawkins): add integer pow support to XLA.
  bits = 6  # Anything more would overflow for any x1 > 1
  acc = ones(shape(x1), dtype=dtype)
  for _ in xrange(bits):
    acc = where(lax.bitwise_and(x2, _constant_like(x2, 1)),
                lax.mul(acc, x1), acc)
    x1 = lax.mul(x1, x1)
    x2 = lax.shift_right_logical(x2, _constant_like(x2, 1))
  return acc


@_wraps(onp.logaddexp)
def logaddexp(x1, x2):
  x1, x2 = _promote_shapes("logaddexp",
                           *_promote_to_result_dtype(onp.logaddexp, x1, x2))
  amax = lax.max(x1, x2)
  return lax.add(amax, lax.log1p(lax.exp(-lax.abs(lax.sub(x1, x2)))))


@_wraps(onp.logaddexp2)
def logaddexp2(x1, x2):
  x1, x2 = _promote_shapes("logaddexp2",
                           *_promote_to_result_dtype(onp.logaddexp2, x1, x2))
  amax = lax.max(x1, x2)
  return lax.add(amax, log2(lax.add(exp2(lax.sub(x1, amax)),
                                    exp2(lax.sub(x2, amax)))))


@_wraps(onp.log2)
def log2(x):
  x, = _promote_to_result_dtype(onp.log2, x)
  return lax.div(lax.log(x), lax.log(_constant_like(x, 2)))


@_wraps(onp.log10)
def log10(x):
  x, = _promote_to_result_dtype(onp.log10, x)
  return lax.div(lax.log(x), lax.log(_constant_like(x, 10)))


@_wraps(onp.exp2)
def exp2(x):
  x, = _promote_to_result_dtype(onp.exp2, x)
  return lax.exp(lax.mul(lax.log(_constant_like(x, 2)), x))


@_wraps(onp.remainder)
def remainder(x1, x2):
  x1, x2 = _promote_args("remainder", x1, x2)
  zero = _constant_like(x1, 0)
  trunc_mod = lax.rem(x1, x2)
  trunc_mod_not_zero = lax.ne(trunc_mod, zero)
  do_plus = lax.bitwise_and(
      lax.ne(lax.lt(trunc_mod, zero), lax.lt(x2, zero)), trunc_mod_not_zero)
  return lax.select(do_plus, lax.add(trunc_mod, x2), trunc_mod)
mod = remainder
fmod = _wraps(onp.fmod)(lambda x, y: lax.rem(x, y))


@_wraps(onp.cbrt)
def cbrt(x):
  x, = _promote_to_result_dtype(onp.cbrt, x)
  return lax.sign(x) * power(lax.abs(x), _constant_like(x, 1. / 3.))


@_wraps(onp.square)
def square(x):
  x, = _promote_to_result_dtype(onp.square, x)
  return x * x


@_wraps(onp.deg2rad)
def deg2rad(x):
  x, = _promote_to_result_dtype(onp.deg2rad, x)
  return lax.mul(x, lax._const(x, pi / 180))


@_wraps(onp.rad2deg)
def rad2deg(x):
  x, = _promote_to_result_dtype(onp.rad2deg, x)
  return lax.mul(x, lax._const(x, 180 / pi))


degrees = rad2deg
radians = deg2rad


@_wraps(onp.heaviside)
def heaviside(x, y):
  x, y = _promote_to_result_dtype(onp.heaviside, x, y)
  zero = lax._const(x, 0)
  return where(lax.lt(x, zero), zero,
               where(lax.gt(x, zero), lax._const(x, 1), y))


@_wraps(onp.hypot)
def hypot(x, y):
  x, y = _promote_to_result_dtype(onp.hypot, x, y)
  return lax.sqrt(x*x + y*y)


@_wraps(onp.reciprocal)
def reciprocal(x):
  x, = _promote_to_result_dtype(onp.reciprocal, x)
  return lax.div(lax._const(x, 1), x)


@_wraps(onp.sinc)
def sinc(x):
  x, = _promote_to_result_dtype(onp.sinc, x)
  pi_x = lax.mul(lax._const(x, pi), x)
  return where(lax.eq(x, lax._const(x, 0)),
               lax._const(x, 1), lax.div(lax.sin(pi_x), pi_x))


@_wraps(onp.arcsinh)
@custom_transforms
def arcsinh(x):
  # asinh(x) = log(x + sqrt(x**2 + 1))
  x, = _promote_to_result_dtype(onp.arcsinh, x)
  one = lax._const(x, 1)
  result = lax.log(x + lax.sqrt(x * x + one))
  if onp.issubdtype(_dtype(result), onp.complexfloating):
    return result
  a = abs(x)
  sqrt_max_value = onp.sqrt(onp.finfo(_dtype(x)).max)
  log2 = lax._const(a, onp.log(2))
  return lax.select(a < sqrt_max_value, result, lax.sign(x) * (lax.log(a) + log2))
defjvp(arcsinh, lambda g, ans, x: g / lax.sqrt(lax._const(x, 1) + square(x)))


@_wraps(onp.arccosh)
def arccosh(x):
  # acosh(x) = log(x + sqrt((x + 1) * (x - 1))) if x < sqrt_max_value
  #            log(x) + log(2) otherwise
  x, = _promote_to_result_dtype(onp.arccosh, x)
  one = lax._const(x, 1)
  result = lax.log(x + lax.sqrt((x + one) * (x - one)))
  if onp.issubdtype(_dtype(result), onp.complexfloating):
    return result
  sqrt_max_value = onp.sqrt(onp.finfo(_dtype(x)).max)
  log2 = lax._const(x, onp.log(2))
  return lax.select(x < sqrt_max_value, result, lax.log(x) + log2)


@_wraps(onp.arctanh)
def arctanh(x):
  # atanh(x) = 0.5 * log((1 + x) / (1 - x))
  x, = _promote_to_result_dtype(onp.arctanh, x)
  one = lax._const(x, 1)
  result = lax._const(x, 0.5) * lax.log((one + x) / (one - x))
  if onp.issubdtype(_dtype(result), onp.complexfloating):
    return result
  return lax.select(abs(x) <= 1, result, lax.full_like(x, onp.nan))


@_wraps(onp.transpose)
def transpose(x, axes=None):
  axes = onp.arange(ndim(x))[::-1] if axes is None else axes
  return lax.transpose(x, axes)


@_wraps(onp.rot90)
def rot90(m, k=1, axes=(0, 1)):
  ax1, ax2 = axes
  ax1 = _canonicalize_axis(ax1, m.ndim)
  ax2 = _canonicalize_axis(ax2, m.ndim)
  if ax1 == ax2:
    raise ValueError("Axes must be different")  # same as numpy error
  k = k % 4
  if k == 0:
    return m
  elif k == 2:
    return flip(flip(m, ax1), ax2)
  else:
    perm = list(range(m.ndim))
    perm[ax1], perm[ax2] = perm[ax2], perm[ax1]
    if k == 1:
      return transpose(flip(m, ax2), perm)
    else:
      return flip(transpose(m, perm), ax2)


@_wraps(onp.flip)
def flip(m, axis):
  return lax.rev(m, [_canonicalize_axis(axis, len(m.shape))])


@_wraps(onp.fliplr)
def fliplr(m):
  return flip(m, 1)


@_wraps(onp.flipud)
def flipud(m):
  return flip(m, 0)


@_wraps(onp.conjugate)
def conjugate(x):
  return lax.conj(x) if iscomplexobj(x) else x
conj = conjugate


@_wraps(onp.imag)
def imag(x):
  return lax.imag(x) if iscomplexobj(x) else zeros_like(x)


@_wraps(onp.real)
def real(x):
  return lax.real(x) if iscomplexobj(x) else x


@_wraps(onp.iscomplex)
def iscomplex(x):
  i = imag(x)
  return lax.ne(i, lax._const(i, 0))

@_wraps(onp.isreal)
def isreal(x):
  i = imag(x)
  return lax.eq(i, lax._const(i, 0))

@_wraps(onp.angle)
def angle(x):
  re = real(x)
  im = imag(x)
  dtype = _dtype(re)
  if not issubdtype(dtype, inexact) or (
      issubdtype(_dtype(x), floating) and ndim(x) == 0):
    dtype = xla_bridge.canonicalize_dtype(float64)
    re = lax.convert_element_type(re, dtype)
    im = lax.convert_element_type(im, dtype)
  return lax.atan2(im, re)


@_wraps(onp.diff)
def diff(a, n=1, axis=-1,):
  if not isinstance(a, ndarray) or a.ndim == 0:
    return a
  if n == 0:
    return a
  if n < 0:
    raise ValueError(
      "order must be non-negative but got " + repr(n))

  nd = a.ndim

  slice1 = [slice(None)] * nd
  slice2 = [slice(None)] * nd
  slice1[axis] = slice(1, None)
  slice2[axis] = slice(None, -1)
  slice1 = tuple(slice1)
  slice2 = tuple(slice2)

  op = not_equal if a.dtype == onp.bool_ else subtract
  for _ in range(n):
    a = op(a[slice1], a[slice2])

  return a


@_wraps(onp.isrealobj)
def isrealobj(a):
  return not iscomplexobj(a)


@_wraps(onp.reshape)
def reshape(a, newshape, order="C"):
  try:
    return a.reshape(newshape, order=order)  # forward to method for ndarrays
  except AttributeError:
    return _reshape(a, newshape, order=order)

def _reshape(a, newshape, order="C"):
  dummy_val = onp.broadcast_to(0, shape(a))  # zero strides
  computed_newshape = onp.reshape(dummy_val, newshape).shape

  if order == "C":
    return lax.reshape(a, computed_newshape, None)
  elif order == "F":
    dims = onp.arange(ndim(a))[::-1]
    return lax.reshape(a, computed_newshape[::-1], dims).T
  elif order == "A":
    raise NotImplementedError("np.reshape order=A is not implemented.")
  else:
    raise ValueError("Unexpected value for 'order' argument: {}.".format(order))

def _reshape_method(a, *newshape, **kwargs):
  order = kwargs.pop("order", "C")
  if len(kwargs) == 1:
    invalid_kwarg, = kwargs
    msg = "'{}' is an invalid keyword argument for this function"
    raise TypeError(msg.format(invalid_kwarg))  # same as NumPy error
  elif kwargs:
    invalid_kwargs = "'{}'".format("'".join(kwargs))
    msg = "{} are invalid keyword arguments for this function"
    raise TypeError(msg.format(invalid_kwargs))  # different from NumPy error
  if len(newshape) == 1 and not isinstance(newshape[0], int):
    newshape = newshape[0]
  return _reshape(a, newshape, order=order)


@_wraps(onp.ravel)
def ravel(a, order="C"):
  if order == "K":
    raise NotImplementedError("Ravel not implemented for order='K'.")
  return reshape(a, (size(a),), order)


@_wraps(onp.squeeze)
def squeeze(a, axis=None):
  if 1 not in shape(a):
    return a
  if axis is None:
    newshape = [d for d in shape(a) if d != 1]
  else:
    if isinstance(axis, int):
      axis = (axis,)
    axis = frozenset(_canonicalize_axis(i, ndim(a)) for i in axis)
    newshape = [d for i, d in enumerate(shape(a))
                if d != 1 or i not in axis]
  return lax.reshape(a, newshape)


@_wraps(onp.expand_dims)
def expand_dims(a, axis):
  shape = _shape(a)
  axis = _canonicalize_axis(axis, ndim(a) + 1)
  return lax.reshape(a, shape[:axis] + (1,) + shape[axis:])


@_wraps(onp.swapaxes)
def swapaxes(a, axis1, axis2):
  perm = onp.arange(ndim(a))
  perm[axis1], perm[axis2] = perm[axis2], perm[axis1]
  return lax.transpose(a, perm)


@_wraps(onp.moveaxis)
def moveaxis(a, source, destination):
  if isinstance(source, int):
    source = (source,)
  if isinstance(destination, int):
    destination = (destination,)
  source = tuple(_canonicalize_axis(i, ndim(a)) for i in source)
  destination = tuple(_canonicalize_axis(i, ndim(a)) for i in destination)
  if len(source) != len(destination):
    raise ValueError("Inconsistent number of elements: {} vs {}"
                     .format(len(source), len(destination)))
  perm = [i for i in range(ndim(a)) if i not in source]
  for dest, src in sorted(zip(destination, source)):
    perm.insert(dest, src)
  return lax.transpose(a, perm)


@_wraps(onp.isclose)
def isclose(a, b, rtol=1e-05, atol=1e-08):
  a, b = _promote_args("isclose", asarray(a), asarray(b))
  dtype = _dtype(a)
  if issubdtype(dtype, inexact):
    if issubdtype(dtype, complexfloating):
      dtype = _result_dtype(real, a)
    rtol = lax.convert_element_type(rtol, dtype)
    atol = lax.convert_element_type(atol, dtype)
    out = lax.le(
      lax.abs(lax.sub(a, b)),
      lax.add(atol, lax.mul(rtol, lax.abs(b))))
    return _maybe_numpy_1_13_isclose_behavior(a, out)
  else:
    return lax.eq(a, b)

numpy_version = tuple(map(int, onp.version.version.split('.')))
if numpy_version < (1, 14):
  # see discussion at https://github.com/numpy/numpy/pull/9720
  def _maybe_numpy_1_13_isclose_behavior(a, out):
    if size(out) == 1 and issubdtype(_dtype(a), complexfloating):
      return lax.reshape(out, (1,))
    else:
      return out
else:
  def _maybe_numpy_1_13_isclose_behavior(a, out):
    return out


# The `jit` on `where` exists to avoid materializing constants in cases like
# `np.where(np.zeros(1000), 7, 4)`. In op-by-op mode, we don't want to
# materialize the broadcast forms of scalar arguments.
@_wraps(onp.where)
@jit
def where(condition, x=None, y=None):
  if x is None or y is None:
    raise ValueError("Must use the three-argument form of where().")
  if not onp.issubdtype(_dtype(condition), onp.bool_):
    condition = lax.ne(condition, zeros_like(condition))
  condition, x, y = broadcast_arrays(condition, x, y)
  if not onp.size(x):
    empty, _ = _promote_dtypes(x, y)
    return empty
  else:
    return lax.select(condition, *_promote_dtypes(x, y))


@_wraps(onp.select)
def select(condlist, choicelist, default=0):
  if len(condlist) != len(choicelist):
    msg = "condlist must have length equal to choicelist ({} vs {})"
    raise ValueError(msg.format(len(condlist), len(choicelist)))
  if len(condlist) == 0:
    raise ValueError("condlist must be non-empty")

  output = default
  for cond, choice in zip(condlist[::-1], choicelist[::-1]):
    output = where(cond, choice, output)
  return output


def broadcast_arrays(*args):
  """Like Numpy's broadcast_arrays but doesn't return views."""
  shapes = [shape(arg) for arg in args]
  if len(set(shapes)) == 1:
    return [arg if isinstance(arg, ndarray) or isscalar(arg) else array(arg)
            for arg in args]
  result_shape = lax.broadcast_shapes(*shapes)
  return [broadcast_to(arg, result_shape) for arg in args]


def broadcast_to(arr, shape):
  """Like Numpy's broadcast_to but doesn't necessarily return views."""
  arr = arr if isinstance(arr, ndarray) or isscalar(arr) else array(arr)
  shape = tuple(map(int, shape))
  if _shape(arr) != shape:
    # TODO(mattjj): revise this to call lax.broadcast_in_dim rather than
    # lax.broadcast and lax.transpose
    lax.broadcast_shapes(shape, _shape(arr))  # error checking
    nlead = len(shape) - len(_shape(arr))
    diff, = onp.where(onp.not_equal(shape[nlead:], _shape(arr)))

    new_dims = tuple(range(nlead)) + tuple(nlead + diff)
    kept_dims = tuple(onp.delete(onp.arange(len(shape)), new_dims))
    perm = onp.argsort(new_dims + kept_dims)

    broadcast_dims = onp.take(shape, new_dims)
    squeezed_array = squeeze(arr, diff)
    return lax.transpose(lax.broadcast(squeezed_array, broadcast_dims), perm)
  else:
    return arr


@_wraps(onp.split)
def split(ary, indices_or_sections, axis=0):
  dummy_val = onp.broadcast_to(0, ary.shape)  # zero strides
  subarrays = onp.split(dummy_val, indices_or_sections, axis)  # shapes
  split_indices = onp.cumsum([0] + [onp.shape(sub)[axis] for sub in subarrays])
  starts, ends = [0] * ndim(ary), shape(ary)
  _subval = lambda x, i, v: lax.subvals(x, [(i, v)])
  return [lax.slice(ary, _subval(starts, axis, start), _subval(ends, axis, end))
          for start, end in zip(split_indices[:-1], split_indices[1:])]

def _split_on_axis(onp_fun, axis):
  @_wraps(onp_fun)
  def f(ary, indices_or_sections):
    return split(ary, indices_or_sections, axis=axis)
  return f

vsplit = _split_on_axis(onp.vsplit, axis=0)
hsplit = _split_on_axis(onp.hsplit, axis=1)
dsplit = _split_on_axis(onp.dsplit, axis=2)


@_wraps(onp.clip)
def clip(a, a_min=None, a_max=None):
  if a_min is None and a_max is None:
    raise "At most one of a_min and a_max may be None"
  if a_min is not None:
    if _dtype(a_min) != _dtype(a):
      a_min = lax.convert_element_type(a_min, _dtype(a))
    a = lax.max(a_min, a)
  if a_max is not None:
    if _dtype(a_max) != _dtype(a):
      a_max = lax.convert_element_type(a_max, _dtype(a))
    a = lax.min(a_max, a)
  return a


def _dtype_info(dtype):
  """Helper function for to get dtype info needed for clipping."""
  if onp.issubdtype(dtype, onp.integer):
    return onp.iinfo(dtype)
  return onp.finfo(dtype)


@_wraps(onp.round)
def round(a, decimals=0):
  dtype = _dtype(a)
  if issubdtype(dtype, integer):
    if decimals < 0:
      raise NotImplementedError(
        "integer np.round not implemented for decimals < 0")
    return a  # no-op on integer types

  def _round_float(x):
    if decimals == 0:
      return lax.round(x)

    factor = _constant_like(x, 10 ** decimals)
    return lax.div(lax.round(lax.mul(x, factor)), factor)

  if issubdtype(dtype, complexfloating):
    return lax.complex(_round_float(lax.real(a)), _round_float(lax.imag(a)))
  else:
    return _round_float(a)
around = round


@_wraps(onp.fix)
def fix(x, out=None):
  if out is not None:
    raise ValueError("fix does not support the `out` argument.")
  zero = lax._const(x, 0)
  return where(lax.ge(x, zero), lax.floor(x), lax.ceil(x))

@_wraps(onp.isfinite)
def isfinite(x):
  dtype = _dtype(x)
  if issubdtype(dtype, floating):
    return lax.is_finite(x)
  elif issubdtype(dtype, complexfloating):
    return lax.bitwise_and(lax.is_finite(real(x)), lax.is_finite(imag(x)))
  else:
    return full_like(x, True, dtype=bool_)

@_wraps(onp.isinf)
def isinf(x):
  dtype = _dtype(x)
  if issubdtype(dtype, floating):
    return lax.eq(lax.abs(x), _constant_like(x, inf))
  elif issubdtype(dtype, complexfloating):
    re = lax.real(x)
    im = lax.imag(x)
    return lax.bitwise_or(lax.eq(lax.abs(re), _constant_like(re, inf)),
                          lax.eq(lax.abs(im), _constant_like(im, inf)))
  else:
    return full_like(x, False, dtype=bool_)

def _isposneginf(infinity, x):
  dtype = _dtype(x)
  if issubdtype(dtype, floating):
    return lax.eq(x, _constant_like(x, infinity))
  elif issubdtype(dtype, complexfloating):
    raise ValueError("isposinf/isneginf are not well defined for complex types")
  else:
    return full_like(x, False, dtype=bool_)

isposinf = _wraps(onp.isposinf)(partial(_isposneginf, inf))
isneginf = _wraps(onp.isneginf)(partial(_isposneginf, -inf))

@_wraps(onp.isnan)
def isnan(x):
  return lax.bitwise_and(lax.bitwise_not(isfinite(x)),
                         lax.bitwise_not(isinf(x)))

@_wraps(onp.nan_to_num)
def nan_to_num(x, copy=True):
  del copy
  dtype = _dtype(x)
  if issubdtype(dtype, complexfloating):
    return lax.complex(nan_to_num(lax.real(x)), nan_to_num(lax.imag(x)))
  info = finfo(xla_bridge.canonicalize_dtype(dtype))
  x = where(isnan(x), _constant_like(x, 0), x)
  x = where(isposinf(x), _constant_like(x, info.max), x)
  x = where(isneginf(x), _constant_like(x, info.min), x)
  return x

### Reducers


def _make_reduction(np_fun, op, init_val, preproc=None):
  """Creates reduction function given a binary operation and monoid identity."""

  @_wraps(np_fun)
  def reduction(a, axis=None, dtype=None, out=None, keepdims=False):
    if out is not None:
      raise ValueError("reduction does not support the `out` argument.")

    a = a if isinstance(a, ndarray) else asarray(a)
    a = preproc(a) if preproc else a
    dims = _reduction_dims(a, axis)
    result_dtype = _dtype(np_fun(onp.ones((), dtype=dtype or _dtype(a))))
    if _dtype(a) != result_dtype:
      a = lax.convert_element_type(a, result_dtype)
    result = lax.reduce(a, _reduction_init_val(a, init_val), op, dims)
    if keepdims:
      shape_with_singletons = lax.subvals(shape(a), zip(dims, (1,) * len(dims)))
      result = lax.reshape(result, shape_with_singletons)
    if dtype and onp.dtype(dtype) != onp.dtype(result_dtype):
      result = lax.convert_element_type(result, dtype)
    return result

  return reduction

def _reduction_dims(a, axis):
  if axis is None:
    return onp.arange(ndim(a))
  elif isinstance(axis, (onp.ndarray, tuple, list)):
    return tuple(_canonicalize_axis(x, ndim(a)) for x in axis)
  elif isinstance(axis, int):
    return (_canonicalize_axis(axis, ndim(a)),)
  else:
    raise TypeError("Unexpected type of axis argument: {}".format(type(axis)))

def _reduction_init_val(a, init_val):
  a_dtype = xla_bridge.canonicalize_dtype(_dtype(a))
  if a_dtype == 'bool':
    return onp.array(init_val > 0, dtype=a_dtype)
  try:
    return onp.array(init_val, dtype=a_dtype)
  except OverflowError:
    assert onp.issubdtype(a_dtype, onp.integer)
    sign, iinfo = onp.sign(init_val), onp.iinfo(a_dtype)
    return onp.array(iinfo.min if sign < 0 else iinfo.max, dtype=a_dtype)

_cast_to_bool = partial(lax.convert_element_type, new_dtype=onp.bool_)

sum = _make_reduction(onp.sum, lax.add, 0)
product = prod = _make_reduction(onp.prod, lax.mul, 1)
amax = max = _make_reduction(onp.max, lax.max, -onp.inf)
amin = min = _make_reduction(onp.min, lax.min, onp.inf)
all = alltrue = _make_reduction(onp.all, lax.bitwise_and, True, _cast_to_bool)
any = sometrue = _make_reduction(onp.any, lax.bitwise_or, False, _cast_to_bool)


@_wraps(onp.mean)
def mean(a, axis=None, dtype=None, out=None, keepdims=False):
  if out is not None:
    raise ValueError("mean does not support the `out` argument.")

  if axis is None:
    normalizer = size(a)
  else:
    normalizer = onp.prod(onp.take(shape(a), axis))
  if dtype is None:
    if (onp.issubdtype(_dtype(a), onp.bool_) or
        onp.issubdtype(_dtype(a), onp.integer)):
      dtype = xla_bridge.canonicalize_dtype(onp.float64)
    else:
      dtype = _dtype(a)

  return lax.div(
      sum(a, axis, dtype=dtype, keepdims=keepdims),
      lax.convert_element_type(normalizer, dtype))

@_wraps(onp.average)
def average(a, axis=None, weights=None, returned=False):
    a = asarray(a)

    if weights is None: # Treat all weights as 1
        avg = mean(a, axis=axis)
        if axis is None:
            weights_sum = full((), size(a), dtype=avg.dtype)
        else:
            weights_sum = full_like(avg, a.shape[axis], dtype=avg.dtype)
    else:
        weights = asarray(weights)

        if issubdtype(a.dtype, integer) or issubdtype(a.dtype, bool_):
            out_dtype = xla_bridge.canonicalize_dtype(result_type(a.dtype,
                                                                  weights.dtype,
                                                                  floating))
        else:
            out_dtype = xla_bridge.canonicalize_dtype(result_type(a.dtype, weights.dtype))

        a_shape = shape(a)
        a_ndim = len(a_shape)
        weights_shape = shape(weights)
        axis = None if axis is None else _canonicalize_axis(axis, a_ndim)

        if a_shape != weights_shape:
            # Make sure the dimensions work out
            if axis is None:
                raise ValueError("Axis must be specified when shapes of a and "
                                 "weights differ.")
            if len(weights_shape) != 1:
                raise ValueError("1D weights expected when shapes of a and "
                                 "weights differ.")
            if weights_shape[0] != a_shape[axis]:
                raise ValueError("Length of weights not "
                                 "compatible with specified axis.")

            weights = broadcast_to(weights, (a_ndim - 1) * (1,) + weights_shape)
            weights = moveaxis(weights, -1, axis)

        weights_sum = sum(weights, axis=axis, dtype=out_dtype)
        avg = sum(multiply(a, weights), axis=axis, dtype=out_dtype) / weights_sum

    if returned:
        if avg.shape != weights_sum.shape:
            weights_sum = broadcast_to(weights_sum, avg.shape)
        return avg, weights_sum
    return avg


@_wraps(onp.var)
def var(a, axis=None, dtype=None, out=None, ddof=0, keepdims=False):
  if out is not None:
    raise ValueError("var does not support the `out` argument.")

  if dtype is None:
    if (onp.issubdtype(_dtype(a), onp.bool_) or
        onp.issubdtype(_dtype(a), onp.integer)):
      dtype = xla_bridge.canonicalize_dtype(onp.float64)
  centered = subtract(a, mean(a, axis, dtype=dtype, keepdims=True))
  if iscomplexobj(centered):
    centered = lax.abs(centered)

  if axis is None:
    normalizer = size(a)
  else:
    normalizer = onp.prod(onp.take(shape(a), axis))
  normalizer = normalizer - ddof

  result = sum(lax.mul(centered, centered), axis,
               dtype=dtype, keepdims=keepdims)
  return lax.div(result, lax.convert_element_type(normalizer, _dtype(result)))


@_wraps(onp.std)
def std(a, axis=None, dtype=None, out=None, ddof=0, keepdims=False):
  if out is not None:
    raise ValueError("std does not support the `out` argument.")
  return sqrt(var(a, axis=axis, dtype=dtype, ddof=ddof, keepdims=keepdims))


@_wraps(onp.ptp)
def ptp(a, axis=None, out=None, keepdims=False):
  if out is not None:
    raise ValueError("ptp does not support the `out` argument.")
  x = amax(a, axis=axis, keepdims=keepdims)
  y = amin(a, axis=axis, keepdims=keepdims)
  return lax.sub(x, y)


@_wraps(onp.allclose)
def allclose(a, b, rtol=1e-05, atol=1e-08):
  return all(isclose(a, b, rtol, atol))


@_wraps(onp.count_nonzero)
def count_nonzero(a, axis=None):
  return sum(lax.ne(a, _constant_like(a, 0)), axis=axis,
             dtype=xla_bridge.canonicalize_dtype(onp.int_))


def _make_nan_reduction(onp_reduction, np_reduction, init_val, nan_if_all_nan):
  @_wraps(onp_reduction)
  def nan_reduction(a, axis=None, out=None, keepdims=False, **kwargs):
    out = np_reduction(where(isnan(a), _reduction_init_val(a, init_val), a),
                       axis=axis, out=out, keepdims=keepdims, **kwargs)
    if nan_if_all_nan:
      return where(all(isnan(a), axis=axis, keepdims=keepdims),
                   _constant_like(a, nan), out)
    else:
      return out

  return nan_reduction

nanmin = _make_nan_reduction(onp.nanmin, min, inf, nan_if_all_nan=True)
nanmax = _make_nan_reduction(onp.nanmax, max, -inf, nan_if_all_nan=True)
nansum = _make_nan_reduction(onp.nansum, sum, 0, nan_if_all_nan=False)
nanprod = _make_nan_reduction(onp.nanprod, prod, 1, nan_if_all_nan=False)


def _make_cumulative_reduction(onp_reduction, window_reduce, init_val,
                               squash_nan=False):
  # We want to allow XLA to fuse the pad and reduce-window operators to
  # avoid materializing the padded output.
  # Consider removing `jit` once again if reduce-window is generalized to
  # support arbitrary padding.
  @partial(jit, static_argnums=(1, 2))
  def _cumulative_reduction(a, axis, dtype):
    if axis is None or isscalar(a):
      a = ravel(a)
      axis = 0

    a_shape = list(shape(a))
    num_dims = len(a_shape)

    if axis < 0:
      axis = axis + num_dims
    if axis < 0 or axis >= num_dims:
      raise ValueError(
          "axis {} is out of bounds for array of dimension {}".format(
              axis, num_dims))

    if squash_nan:
      a = where(isnan(a), _constant_like(a, init_val), a)

    if dtype:
      a = lax.convert_element_type(a, dtype)

    if a_shape[axis] == 0:
      return a

    padding = [(0, 0, 0)] * num_dims
    padding[axis] = (a_shape[axis] - 1, 0, 0)
    a = lax.pad(a, _constant_like(a, init_val), padding)
    strides = [1] * num_dims
    window_dims = [1] * num_dims
    window_dims[axis] = a_shape[axis]
    return window_reduce(
       a, window_dims, strides, xla_client.PaddingType.VALID)

  @_wraps(onp_reduction)
  def cumulative_reduction(a, axis=None, dtype=None):
    # jit doesn't support kwargs as static_args.
    return _cumulative_reduction(a, axis, dtype)

  return cumulative_reduction


cumsum = _make_cumulative_reduction(
  onp.cumsum, lax._reduce_window_sum, 0, squash_nan=False)
cumprod = _make_cumulative_reduction(
  onp.cumprod, lax._reduce_window_prod, 1, squash_nan=False)
cumproduct = cumprod
nancumsum = _make_cumulative_reduction(
  onp.nancumsum, lax._reduce_window_sum, 0, squash_nan=True)
nancumprod = _make_cumulative_reduction(
  onp.nancumprod, lax._reduce_window_prod, 1, squash_nan=True)


### Array-creation functions

@partial(jit, static_argnums=(1, 2))
def _pad(array, pad_width, mode, constant_values):
  array = asarray(array)
  nd = ndim(array)
  pad_width = onp.broadcast_to(onp.asarray(pad_width), (nd, 2))
  if any(pad_width < 0):
    raise ValueError("index can't contain negative values")

  if mode == "constant":
    constant_values = broadcast_to(asarray(constant_values), (nd, 2))
    constant_values = lax.convert_element_type(constant_values, array.dtype)
    for i in xrange(nd):
      widths = [(0, 0, 0)] * nd
      widths[i] = (pad_width[i, 0], 0, 0)
      array = lax.pad(array, constant_values[i, 0], widths)
      widths[i] = (0, pad_width[i, 1], 0)
      array = lax.pad(array, constant_values[i, 1], widths)
    return array
  elif mode in ("symmetric", "reflect", "wrap"):
    for i in xrange(nd):
      if array.shape[i] == 0:
        if (pad_width[i, 0] > 0 or pad_width[i, 1] > 0):
          msg = "Cannot apply '{}' padding to empty axis"
          raise ValueError(msg.format(mode))
        continue

      n = array.shape[i]
      rarray = lax.rev(array, dimensions=(i,))
      offset = 1 if (mode == "reflect" and n > 1) else 0
      wrap_mode = mode == "wrap"

      def build_padding(padding, forward):
        xs = []
        delta = n - offset
        while padding > delta:
          padding -= delta
          p = array if forward else rarray
          xs.append(lax.slice_in_dim(p, offset, n, axis=i))
          if not wrap_mode:
            forward = not forward
        if padding > 0:
          x = lax.slice_in_dim(array if forward else rarray, offset,
                               padding + offset, axis=i)
          xs.append(x)
        return xs

      parts = reversed(build_padding(pad_width[i, 0], forward=not wrap_mode))
      parts = [lax.rev(x, dimensions=(i,)) for x in parts]
      parts += [array]
      parts += build_padding(pad_width[i, 1], forward=wrap_mode)
      array = lax.concatenate(parts, dimension=i)
    return array
  else:
    msg = "Unimplemented padding mode '{}' for np.pad."
    raise NotImplementedError(msg.format(mode))

@_wraps(onp.pad)
def pad(array, pad_width, mode='constant', constant_values=0):
  return _pad(array, pad_width, mode, constant_values)


@_wraps(onp.stack)
def stack(arrays, axis=0):
  if not len(arrays):
    raise ValueError("Need at least one array to stack.")
  shape0 = shape(arrays[0])
  axis = _canonicalize_axis(axis, len(shape0) + 1)
  new_shape = list(shape0)
  new_shape.insert(axis, 1)
  new_arrays = []
  for a in arrays:
    if shape(a) != shape0:
      raise ValueError("All input arrays must have the same shape.")
    new_arrays.append(reshape(a, new_shape))
  return concatenate(new_arrays, axis=axis)

@_wraps(onp.tile)
def tile(a, reps):
  if isinstance(reps, int):
    reps = (reps,)
  a = reshape(a, (1,) * (len(reps) - ndim(a)) + shape(a))
  reps = (1,) * (ndim(a) - len(reps)) + tuple(reps)
  for i, rep in enumerate(reps):
    a = concatenate([a] * int(rep), axis=i)
  return a

@_wraps(onp.concatenate)
def concatenate(arrays, axis=0):
  if not len(arrays):
    raise ValueError("Need at least one array to concatenate.")
  if ndim(arrays[0]) == 0:
    raise ValueError("Zero-dimensional arrays cannot be concatenated.")
  axis = _canonicalize_axis(axis, ndim(arrays[0]))
  arrays = _promote_dtypes(*arrays)
  # lax.concatenate can be slow to compile for wide concatenations, so form a
  # tree of concatenations as a workaround especially for op-by-op mode.
  # (https://github.com/google/jax/issues/653).
  k = 16
  while len(arrays) > 1:
    arrays = [lax.concatenate(arrays[i:i+k], axis)
              for i in range(0, len(arrays), k)]
  return arrays[0]


@_wraps(onp.vstack)
def vstack(tup):
  return concatenate([atleast_2d(m) for m in tup], axis=0)
row_stack = vstack


@_wraps(onp.hstack)
def hstack(tup):
  arrs = [atleast_1d(m) for m in tup]
  if arrs[0].ndim == 1:
    return concatenate(arrs, 0)
  return concatenate(arrs, 1)


@_wraps(onp.dstack)
def dstack(tup):
  return concatenate([atleast_3d(m) for m in tup], axis=2)


@_wraps(onp.column_stack)
def column_stack(tup):
  arrays = []
  for v in tup:
    arr = array(v)
    if arr.ndim < 2:
      arr = arr.reshape((-1, 1))
    arrays.append(arr)
  return concatenate(arrays, 1)


@_wraps(onp.atleast_1d)
def atleast_1d(*arys):
  if len(arys) == 1:
    arr = array(arys[0])
    return arr if ndim(arr) >= 1 else reshape(arr, -1)
  else:
    return [atleast_1d(arr) for arr in arys]


@_wraps(onp.atleast_2d)
def atleast_2d(*arys):
  if len(arys) == 1:
    arr = array(arys[0])
    return arr if ndim(arr) >= 2 else reshape(arr, (1, -1))
  else:
    return [atleast_2d(arr) for arr in arys]


@_wraps(onp.atleast_3d)
def atleast_3d(*arys):
  if len(arys) == 1:
    arr = array(arys[0])
    if ndim(arr) <= 1:
      arr = reshape(arr, (1, -1, 1))
    elif ndim(arr) == 2:
      arr = reshape(arr, shape(arr) + (1,))
    return arr
  else:
    return [atleast_3d(arr) for arr in arys]


@_wraps(onp.array)
def array(object, dtype=None, copy=True, order="K", ndmin=0):
  if order is not None and order != "K":
    raise NotImplementedError("Only implemented for order='K'")
  lax._check_user_dtype_supported(dtype, "array")

  if isinstance(object, ndarray):
    if dtype and _dtype(object) != xla_bridge.canonicalize_dtype(dtype):
      out = lax.convert_element_type(object, dtype)
    else:
      out = device_put(object)
  elif hasattr(object, '__array__'):
    # this case is for duck-typed handling of objects that implement `__array__`
    out = array(object.__array__(), dtype and xla_bridge.canonicalize_dtype(dtype))
  elif isinstance(object, (list, tuple)):
    if object:
      out = stack([array(elt, dtype=dtype) for elt in object])
    else:
      out = onp.array([], dtype)
  elif isscalar(object):
    out = lax.reshape(object, ())
    if dtype and _dtype(out) != xla_bridge.canonicalize_dtype(dtype):
      out = lax.convert_element_type(out, dtype)
  else:
    try:
      view = memoryview(object)
    except TypeError:
      pass  # `object` does not support the buffer interface.
    else:
      return array(onp.asarray(view), dtype, copy)

    raise TypeError("Unexpected input type for array: {}".format(type(object)))

  if ndmin > ndim(out):
    out = lax.reshape(out, (1,) * (ndmin - ndim(out)) + shape(out))
  return out

@_wraps(onp.asarray)
def asarray(a, dtype=None, order=None):
  lax._check_user_dtype_supported(dtype, "asarray")
  return array(a, dtype=dtype, copy=False, order=order)


@_wraps(onp.zeros_like)
def zeros_like(x, dtype=None):
  lax._check_user_dtype_supported(dtype, "zeros_like")
  return lax.full_like(x, 0, dtype)


@_wraps(onp.ones_like)
def ones_like(x, dtype=None):
  lax._check_user_dtype_supported(dtype, "ones_like")
  return lax.full_like(x, 1, dtype)


@_wraps(onp.full)
def full(shape, fill_value, dtype=None):
  lax._check_user_dtype_supported(dtype, "full")
  return lax.full(shape, fill_value, dtype)


@_wraps(onp.full_like)
def full_like(a, fill_value, dtype=None):
  lax._check_user_dtype_supported(dtype, "full_like")
  return lax.full_like(a, fill_value, dtype)


@_wraps(onp.zeros)
def zeros(shape, dtype=None):
  if isinstance(shape, types.GeneratorType):
    raise TypeError("expected sequence object with len >= 0 or a single integer")
  lax._check_user_dtype_supported(dtype, "zeros")
  dtype = onp.dtype("float64") if dtype is None else dtype
  shape = (shape,) if onp.isscalar(shape) else shape
  return lax.full(shape, 0, dtype)

@_wraps(onp.ones)
def ones(shape, dtype=None):
  if isinstance(shape, types.GeneratorType):
    raise TypeError("expected sequence object with len >= 0 or a single integer")
  lax._check_user_dtype_supported(dtype, "ones")
  dtype = onp.dtype("float64") if dtype is None else dtype
  shape = (shape,) if onp.isscalar(shape) else shape
  return lax.full(shape, 1, dtype)


@_wraps(onp.array_equal)
def array_equal(a1, a2):
  try:
    a1, a2 = asarray(a1), asarray(a2)
  except Exception:
    return False
  return shape(a1) == shape(a2) and all(asarray(a1 == a2))


# We can't create uninitialized arrays in XLA; use zeros for empty.
empty_like = zeros_like
empty = zeros


@_wraps(onp.eye)
def eye(N, M=None, k=None, dtype=None):
  lax._check_user_dtype_supported(dtype, "eye")
  dtype = onp.dtype("float64") if dtype is None else dtype
  M = N if M is None else M
  if N < 0 or M < 0:
    msg = "negative dimensions are not allowed, got {} and {}"
    raise ValueError(msg.format(N, M))
  if k is None:
    return lax.broadcasted_eye(dtype, (N, M), (0, 1))
  else:
    k_dtype = _dtype(k)
    if not onp.issubdtype(k_dtype, onp.integer):
      msg = "eye argument `k` must be of integer dtype, got {}"
      raise TypeError(msg.format(k_dtype))
    rows = k + lax.broadcasted_iota(k_dtype, (N, M), 0)
    cols = lax.broadcasted_iota(k_dtype, (N, M), 1)
    return lax.convert_element_type(lax.eq(rows, cols), dtype)


@_wraps(onp.identity)
def identity(n, dtype=None):
  lax._check_user_dtype_supported(dtype, "identity")
  return eye(n, dtype=dtype)


@_wraps(onp.arange)
def arange(start, stop=None, step=None, dtype=None):
  lax._check_user_dtype_supported(dtype, "arange")
  # If called like np.arange(N), we create a lazy lax._IotaConstant.
  if stop is None and step is None:
    dtype = dtype or _dtype(start)
    if onp.issubdtype(dtype, onp.integer):
      return lax.iota(dtype, start)  # avoids materializing

  # Fall back to instantiating an ndarray in host memory
  return onp.arange(start, stop=stop, step=step, dtype=dtype)

def _wrap_numpy_nullary_function(f):
  """Adapts `f` to return a DeviceArray instead of an onp.ndarray.

  `f` cannot have any non-static array arguments.
  """
  @_wraps(f)
  def wrapper(*args, **kwargs):
    return asarray(f(*args, **kwargs))
  return wrapper

def linspace(start, stop, num=50, endpoint=True, retstep=False, dtype=None,
             axis=0):
  lax._check_user_dtype_supported(dtype, "linspace")
  try:
    out = onp.linspace(start, stop, num, endpoint, retstep, dtype, axis)
    if retstep:
      return asarray(out[0]), out[1]
    else:
      return asarray(out)
  except TypeError:  # Old versions of onp may lack axis arg.
    out = onp.linspace(start, stop, num, endpoint, retstep, dtype)
    if retstep:
      return moveaxis(asarray(out[0]), 0, axis), out[1]
    else:
      return moveaxis(asarray(out), 0, axis)

logspace = _wrap_numpy_nullary_function(onp.logspace)
geomspace = _wrap_numpy_nullary_function(onp.geomspace)

@_wraps(onp.meshgrid)
def meshgrid(*args, **kwargs):
  indexing = kwargs.get("indexing", "xy")
  sparse = kwargs.get("sparse", False)
  copy = kwargs.get("copy", True)
  if not copy:
    raise ValueError("jax.numpy.meshgrid only supports copy=True")

  args = list(args)
  if indexing == "xy":
    if len(args) >= 2:
      args[0], args[1] = args[1], args[0]
  elif indexing != "ij":
    raise ValueError("Valid values for indexing are 'xy' and 'ij', got {}"
                     .format(indexing))

  shape = []
  for i, a in enumerate(args):
    args[i] = a = asarray(a)
    if len(a.shape) != 1:
      msg = "Arguments to jax.numpy.meshgrid must be 1D, got shape {}"
      raise ValueError(msg.format(a.shape))
    shape.append(1 if sparse else a.shape[0])

  output = []
  for i, a in enumerate(args):
    a = asarray(a)
    s = shape
    if sparse:
      s = list(s)
      s[i] = a.shape[0]
    output.append(lax.broadcast_in_dim(a, s, (i,)))

  if indexing == "xy" and len(args) >= 2:
      output[0], output[1] = output[1], output[0]

  return output


@_wraps(onp.ix_)
def ix_(*args):
  n = len(args)
  output = []
  for i, a in enumerate(args):
    a = asarray(a)
    if len(a.shape) != 1:
      msg = "Arguments to jax.numpy.ix_ must be 1-dimensional, got shape {}"
      raise ValueError(msg.format(a.shape))
    if _dtype(a) == bool_:
      raise NotImplementedError(
        "Boolean arguments to jax.numpy.ix_ are not implemented")
    shape = [1] * n
    shape[i] = a.shape[0]
    if a.size == 0:
      # Numpy uses an integer index type for empty arrays.
      output.append(lax.full(shape, onp.zeros((), onp.intp)))
    else:
      output.append(lax.reshape(a, shape))
  return tuple(output)



def _repeat_scalar(a, repeats, axis=None):
  if not isscalar(repeats):
    raise NotImplementedError(
        "_repeat_scalar implementation only supports scalar repeats")
  if axis is None or isscalar(a):
    a = ravel(a)
    axis = 0
  a_shape = list(shape(a))
  num_dims = len(a_shape)
  if axis < 0:
    axis = axis + num_dims

  if axis < 0 or axis >= num_dims:
    raise ValueError(
        "axis {} is out of bounds for array of dimension {}".format(
            axis, num_dims))

  # Broadcasts to [..., X, repeats, ...] and reshapes to [..., X * repeats, ...]
  broadcast_shape = list(a_shape)
  broadcast_shape.insert(axis + 1, repeats)
  broadcast_dims = onp.concatenate((onp.arange(0, axis + 1),
                                    onp.arange(axis + 2, num_dims + 1)))
  a_shape[axis] *= repeats
  return lax.reshape(
      lax.broadcast_in_dim(a, broadcast_shape, broadcast_dims),
      a_shape)

@_wraps(onp.repeat)
def repeat(a, repeats, axis=None):
  '''
  :param repeats: int or array of ints
  '''
  # use `_repeat_scalar` when possible
  if isscalar(repeats):
    return _repeat_scalar(a, repeats, axis)
  repeats_raveled = ravel(array(repeats)) # make sure it's jax's array type
  if size(repeats_raveled) == 1:
    return _repeat_scalar(a, list(repeats_raveled)[0], axis)

  if axis is None or isscalar(a):
    a = ravel(a)
    axis = 0

  # repeats must match the dimension along the requested axis
  a_shape = list(a.shape)
  n = a_shape[axis]
  if size(repeats_raveled) != n:
    raise ValueError("repeats shape {} does not match the dimension on axis {}".format(
      repeats_raveled.shape, n
    ))

  # calculating the new shape
  total = sum(repeats_raveled)

  new_shape = a_shape[:]
  new_shape[axis] = total

  a_flattened = ravel(a)

  '''
  main algorithm:
  first break down raveled input array into list of chunks; each chunk is the unit of repeat
  then tile the repeats to have same length as the list of chunks
  finally repeat each unit x number of times according to the tiled repeat list
  '''
  chunks = product(a_shape[:axis+1]).item()
  a_splitted = split(a_flattened, chunks)
  repeats_tiled = tile(repeats_raveled, chunks // len(repeats_raveled))

  ret = array([], dtype=a.dtype)
  for i, repeat in enumerate(repeats_tiled):
    if not isinstance(repeat, int):
      repeat = repeat.item()
    ret = concatenate((ret, tile(a_splitted[i], repeat)))

  return reshape(ret, new_shape)

@_wraps(onp.tri)
def tri(N, M=None, k=0, dtype=None):
  lax._check_user_dtype_supported(dtype, "tri")
  M = M if M is not None else N
  dtype = dtype or float32
  x = arange(N, dtype=int32)
  y = arange(M, dtype=int32)
  mask = lax.ge(
      (lax.broadcast_in_dim(x, shape=(N, M), broadcast_dimensions=(0,)) +
       int32(k)),
      lax.broadcast(y, [N]))
  return lax.convert_element_type(mask, dtype)


@_wraps(onp.tril)
def tril(m, k=0):
  m_shape = shape(m)
  if len(m_shape) < 2:
    raise ValueError("Argument to jax.numpy.tril must be at least 2D")
  mask = tri(*m_shape[-2:], k=k, dtype=bool)
  return lax.select(lax.broadcast(mask, m_shape[:-2]), m, zeros_like(m))


@_wraps(onp.triu)
def triu(m, k=0):
  m_shape = shape(m)
  if len(m_shape) < 2:
    raise ValueError("Argument to jax.numpy.triu must be at least 2D")
  mask = tri(*m_shape[-2:], k=k - 1, dtype=bool)
  return lax.select(lax.broadcast(mask, m_shape[:-2]), zeros_like(m), m)


@_wraps(onp.trace)
def trace(a, offset=0, axis1=0, axis2=1, dtype=None, out=None):
  if out:
    raise NotImplementedError("The 'out' argument to trace is not supported.")
  lax._check_user_dtype_supported(dtype, "trace")

  axis1 = _canonicalize_axis(axis1, ndim(a))
  axis2 = _canonicalize_axis(axis2, ndim(a))

  a_shape = shape(a)
  if dtype is None:
    dtype = _dtype(a)
    if issubdtype(dtype, integer):
      default_int = xla_bridge.canonicalize_dtype(onp.int_)
      if iinfo(dtype).bits < iinfo(default_int).bits:
        dtype = default_int

  # Move the axis? dimensions to the end.
  perm = [i for i in range(len(a_shape)) if i != axis1 and i != axis2]
  perm = perm + [axis1, axis2]
  a = lax.transpose(a, perm)

  # Mask out the diagonal and reduce.
  a = where(eye(a_shape[axis1], a_shape[axis2], k=offset, dtype=bool),
            a, zeros_like(a))
  return sum(a, axis=(-2, -1), dtype=dtype)


def _wrap_indices_function(f):
  @_wraps(f)
  def wrapper(*args, **kwargs):
    return tuple(asarray(x) for x in f(*args, **kwargs))
  return wrapper

diag_indices = _wrap_indices_function(onp.diag_indices)
tril_indices = _wrap_indices_function(onp.tril_indices)
triu_indices = _wrap_indices_function(onp.triu_indices)
mask_indices = _wrap_indices_function(onp.mask_indices)


@_wraps(onp.diagonal)
def diagonal(a, offset=0, axis1=0, axis2=1):
  a_shape = shape(a)
  a_ndims = len(a_shape)

  # Move the two dimensions to the end.
  axis1 = _canonicalize_axis(axis1, a_ndims)
  axis2 = _canonicalize_axis(axis2, a_ndims)
  perm = [i for i in range(a_ndims) if i != axis1 and i != axis2]
  perm = perm + [axis1, axis2]
  a = lax.transpose(a, perm)

  # Mask out the diagonal and reduce over one of the axes
  a = where(eye(a_shape[axis1], a_shape[axis2], k=offset, dtype=bool),
            a, zeros_like(a))
  reduce_axis = -2 if offset < 0 else -1
  d = sum(a, axis=reduce_axis, dtype=_dtype(a))

  # Slice out the correct diagonal size.
  diag_size = _max(0, _min(a_shape[axis1] + _min(offset, 0),
                           a_shape[axis2] - _max(offset, 0)))
  return lax.slice_in_dim(d, 0, diag_size, axis=-1)


@_wraps(onp.diag)
def diag(v, k=0):
  v_shape = shape(v)
  if len(v_shape) == 1:
    zero = lambda x: lax.full_like(x, shape=(), fill_value=0)
    n = v_shape[0] + _abs(k)
    v = lax.pad(v, zero(v), ((_max(0, k), _max(0, -k), 0),))
    return where(eye(n, k=k, dtype=bool), v, zeros_like(v))
  elif len(v_shape) == 2:
    return diagonal(v, offset=k)
  else:
    raise ValueError("diag input must be 1d or 2d")


@_wraps(onp.polyval)
def polyval(p, x):
  if isinstance(p, onp.poly1d):
    p = onp.asarray(p)
  if isinstance(x, onp.poly1d):
    y = 0
  else:
    y = zeros_like(x)
  for i in range(len(p)):
    y = y * x + p[i]
  return y


@_wraps(onp.append)
def append(arr, values, axis=None):
  if axis is None:
    return concatenate([ravel(arr), ravel(values)], 0)
  else:
    return concatenate([arr, values], axis=axis)


### Tensor contraction operations


@_wraps(onp.dot)
def dot(a, b):  # pylint: disable=missing-docstring
  _check_arraylike("dot", a, b)
  a, b = _promote_dtypes(a, b)
  a_ndim, b_ndim = ndim(a), ndim(b)
  if a_ndim == 0 or b_ndim == 0:
    return lax.mul(a, b)
  if _max(a_ndim, b_ndim) <= 2:
    return lax.dot(a, b)

  if b_ndim == 1:
    contract_dims = ((a_ndim - 1,), (0,))
  else:
    contract_dims = ((a_ndim - 1,), (b_ndim - 2,))
  batch_dims = ((), ())
  return lax.dot_general(a, b, (contract_dims, batch_dims))


@_wraps(onp.matmul)
def matmul(a, b):  # pylint: disable=missing-docstring
  _check_arraylike("matmul", a, b)
  a_is_vec, b_is_vec = (ndim(a) == 1), (ndim(b) == 1)
  a = lax.reshape(a, (1,) + shape(a)) if a_is_vec else a
  b = lax.reshape(b, shape(b) + (1,)) if b_is_vec else b

  a, b = _promote_dtypes(a, b)
  batch_shape = lax.broadcast_shapes(shape(a)[:-2], shape(b)[:-2])
  a = broadcast_to(a, batch_shape + shape(a)[-2:])
  b = broadcast_to(b, batch_shape + shape(b)[-2:])
  batch_dims = tuple(range(len(batch_shape)))
  result = lax.dot_general(a, b, (((ndim(a) - 1,), (ndim(b) - 2,)),
                                  (batch_dims, batch_dims)))

  if a_is_vec or b_is_vec:
    m, n = shape(result)[-2:]
    new_m = () if a_is_vec else (m,)
    new_n = () if b_is_vec else (n,)
    return lax.reshape(result, batch_shape + new_m + new_n)
  else:
    return result


@_wraps(onp.vdot)
def vdot(a, b):
  if onp.issubdtype(_dtype(a), onp.complexfloating):
    a = conj(a)
  return dot(a.ravel(), b.ravel())


@_wraps(onp.tensordot)
def tensordot(a, b, axes=2):
  _check_arraylike("tensordot", a, b)
  if not (ndim(a) >= 1 and ndim(b) >= 1):
    msg = "tensordot requires a.ndim and b.dim to be at least 1, got {} and {}."
    raise TypeError(msg.format(ndim(a), ndim(b)))

  if type(axes) is int:
    if axes == 0:
      a, b = _promote_dtypes(a, b)
      return lax.mul(lax.reshape(a, shape(a) + (1,) * ndim(b)),
                     lax.reshape(b, (1,) * ndim(a) + shape(b)))
    else:
      a, b = _promote_dtypes(a, b)
      a_reshape = lax.reshape(a, (_prod(a.shape[:-axes]), _prod(a.shape[-axes:])))
      b_reshape = lax.reshape(b, (_prod(b.shape[:axes]), _prod(b.shape[axes:])))
      out_reshape = lax.dot(a_reshape, b_reshape)
      return lax.reshape(out_reshape, a.shape[:-axes] + b.shape[axes:])
  elif type(axes) in (list, tuple) and len(axes) == 2:
    ax1, ax2 = axes
    if type(ax1) == type(ax2) == int:
      a_transposed = moveaxis(a, ax1, -1) if ax1 != a.ndim - 1 else a
      b_transposed = moveaxis(b, ax2, 0) if ax2 != 0 else b
      return tensordot(a_transposed, b_transposed, 1)
    elif type(ax1) in (list, tuple) and type(ax2) in (list, tuple):
      if len(ax1) != len(ax2):
        msg = "tensordot requires axes lists to have equal length, got {} and {}."
        raise TypeError(msg.format(ax1, ax2))
      num_axes = len(ax1)
      a_transposed = moveaxis(a, ax1, tuple(range(a.ndim - num_axes, a.ndim)))
      b_transposed = moveaxis(b, ax2, tuple(range(num_axes)))
      return tensordot(a_transposed, b_transposed, num_axes)
  msg = ("tensordot axes argument must be an int, a pair of ints, or a pair of "
         "lists/tuples of ints.")
  raise TypeError(msg)


@_wraps(onp.einsum)
def einsum(*operands, **kwargs):
  optimize = kwargs.pop('optimize', 'auto')
  optimize = 'greedy' if optimize is True else optimize
  if kwargs:
    msg = 'invalid keyword arguments for einsum: {}'
    raise TypeError(msg.format(', '.join(kwargs)))
  # using einsum_call=True here is an internal api for opt_einsum
  operands, contractions = opt_einsum.contract_path(
      *operands, einsum_call=True, use_blas=True, optimize=optimize)
  contractions = tuple(data[:3] for data in contractions)
  return _einsum(operands, contractions)

@_wraps(onp.einsum_path)
def einsum_path(subscripts, *operands, **kwargs):
  optimize = kwargs.pop('optimize', 'greedy')
  # using einsum_call=True here is an internal api for opt_einsum
  return opt_einsum.contract_path(subscripts, *operands, optimize=optimize)

@partial(jit, static_argnums=(1,))
def _einsum(operands, contractions):
  operands = list(_promote_dtypes(*operands))
  sum = lambda x, axes: lax.reduce(x, onp.array(0, x.dtype), lax.add, axes)

  def sum_uniques(operand, names, uniques):
    if uniques:
      axes = [names.index(name) for name in uniques]
      operand = sum(operand, axes)
      names = removechars(names, uniques)
    return operand, names

  def sum_repeats(operand, names, counts, keep_names):
    for name, count in counts.items():
      if count > 1:
        axes = [i for i, n in enumerate(names) if n == name]
        eye = lax.broadcasted_eye(operand.dtype, operand.shape, axes)
        if name not in keep_names:
          operand = sum(operand * eye, axes)
          names = names.replace(name, '')
        else:
          operand = sum(operand * eye, axes[:-1])
          names = names.replace(name, '', count - 1)
    return operand, names

  for operand_indices, contracted_names, einstr in contractions:
    input_str, result_names = einstr.split('->')
    input_names = input_str.split(',')

    # switch on the number of operands to be processed in this loop iteration.
    # every case here sets 'operand' and 'names'.
    if len(operand_indices) == 1:
      operand = operands.pop(operand_indices[0])
      names, = input_names
      counts = collections.Counter(names)

      # sum out unique contracted indices with a single reduce-sum
      uniques = [name for name in contracted_names if counts[name] == 1]
      operand, names = sum_uniques(operand, names, uniques)

      # for every repeated index, do a contraction against an identity matrix
      operand, names = sum_repeats(operand, names, counts, result_names)

    elif len(operand_indices) == 2:
      lhs, rhs = map(operands.pop, operand_indices)
      lhs_counts, rhs_counts = map(collections.Counter, input_names)
      lhs_names, rhs_names = input_names

      # sum out unique contracted indices in lhs and rhs
      lhs_uniques = [name for name in contracted_names
                     if lhs_counts[name] == 1 and rhs_counts[name] == 0]
      lhs, lhs_names = sum_uniques(lhs, lhs_names, lhs_uniques)

      rhs_uniques = [name for name in contracted_names
                     if rhs_counts[name] == 1 and lhs_counts[name] == 0]
      rhs, rhs_names = sum_uniques(rhs, rhs_names, rhs_uniques)

      # for every repeated index, contract against an identity matrix
      lhs, lhs_names = sum_repeats(lhs, lhs_names, lhs_counts,
                                   result_names + rhs_names)
      rhs, rhs_names = sum_repeats(rhs, rhs_names, rhs_counts,
                                   result_names + lhs_names)

      contracted_names = contracted_names & (set(lhs_names) | set(rhs_names))
      batch_names = (set(lhs_names) & set(rhs_names)) - contracted_names
      lhs_batch, rhs_batch = unzip2((lhs_names.find(n), rhs_names.find(n))
                                    for n in batch_names)

      # NOTE(mattjj): this can fail non-deterministically in python3, maybe
      # due to opt_einsum
      assert _all(name in lhs_names and name in rhs_names and
                  lhs.shape[lhs_names.index(name)] == rhs.shape[rhs_names.index(name)]
                  for name in contracted_names)

      # move batch dims to the front (required by lax.dot_general, and easier)
      batch_dims = tuple(range(len(batch_names)))
      if lhs_batch != rhs_batch or set(lhs_batch) != set(batch_dims):
        lhs = moveaxis(lhs, lhs_batch, batch_dims)
        lhs_names = _movechars(lhs_names, lhs_batch, batch_dims)
        rhs = moveaxis(rhs, rhs_batch, batch_dims)
        rhs_names = _movechars(rhs_names, rhs_batch, batch_dims)
        batch_names = ''.join(batch_names)
      else:
        batch_dims = tuple(lhs_batch)
        batch_names = ''.join(lhs_names[i] for i in range(len(lhs_names))
                              if i in batch_dims)

      if contracted_names:
        # contract using lax.dot_general
        lhs_cont, rhs_cont = unzip2((lhs_names.index(n), rhs_names.index(n))
                                    for n in contracted_names)
        operand = _dot_general(lhs, rhs, lhs_cont, rhs_cont, len(batch_dims))
        deleted_names = batch_names + ''.join(contracted_names)
        names = (batch_names + removechars(lhs_names, deleted_names)
                 + removechars(rhs_names, deleted_names))
      else:
        # no contraction, just a tensor product
        nbatch = len(batch_names)
        assert lhs.shape[:nbatch] == rhs.shape[:nbatch]
        names = batch_names + lhs_names[nbatch:] + rhs_names[nbatch:]
        lhs_shape = lhs.shape + (1,) * (rhs.ndim - nbatch)
        rhs_shape = rhs.shape[:nbatch] + (1,) * (lhs.ndim - nbatch) + rhs.shape[nbatch:]
        operand = lax.reshape(lhs, lhs_shape) * lax.reshape(rhs, rhs_shape)

    else:
      raise NotImplementedError  # if this is actually reachable, open an issue!

    # the resulting 'operand' with axis labels 'names' should be a permutation
    # of the desired result
    assert len(names) == len(result_names) == len(set(names))
    assert set(names) == set(result_names)
    if names != result_names:
      perm = tuple([names.index(name) for name in result_names])
      operand = lax.transpose(operand, perm)
    operands.append(operand)  # used in next iteration

  return operands[0]


def _dot_general(lhs, rhs, lhs_cont, rhs_cont, nbatch):
  """Helper for einsum contractions."""
  # lax.dot_general has some tight constraints on dimension_numbers that this
  # wrapper loosens via transposes and reshapes
  assert len(lhs_cont) == len(rhs_cont) > 0
  ncont = len(lhs_cont)
  lhs_ntensor = lhs.ndim - nbatch - ncont
  rhs_ntensor = rhs.ndim - nbatch - ncont
  batch_dims = tuple(range(nbatch))

  if ncont == 1 and 0 <= lhs_ntensor <= 1 and 0 <= rhs_ntensor <= 1:
    dimension_numbers = [(lhs_cont, rhs_cont), (batch_dims, batch_dims)]
    return lax.dot_general(lhs, rhs, dimension_numbers)
  else:
    # move contracting dimensions to the end. lax.dot_general only allows one
    # contracting dimension, so if there's more than one we collapse them.
    if ncont > 1:
      lhs_cdims = tuple(range(lhs.ndim - ncont, lhs.ndim))
      lhs = moveaxis(lhs, lhs_cont, lhs_cdims)
      lhs = lhs.reshape(lhs.shape[:-ncont] + (-1,))

      rhs_cdims = tuple(range(rhs.ndim - ncont, rhs.ndim))
      rhs = moveaxis(rhs, rhs_cont, rhs_cdims)
      rhs = rhs.reshape(rhs.shape[:-ncont] + (-1,))
    else:
      lhs = moveaxis(lhs, lhs_cont[0], -1)
      rhs = moveaxis(rhs, rhs_cont[0], -1)

    # lax.dot_general only allows zero or one tensor product dims per operand,
    # so if there's more than one we collapse them.
    result_shape = lhs.shape[:nbatch] + lhs.shape[nbatch:-1] + rhs.shape[nbatch:-1]

    if lhs_ntensor > 1:
      lhs = lhs.reshape(lhs.shape[:nbatch] + (-1,) + lhs.shape[-1:])

    if rhs_ntensor > 1:
      rhs = rhs.reshape(rhs.shape[:nbatch] + (-1,) + rhs.shape[-1:])

    lhs_cont, rhs_cont = [lhs.ndim - 1], [rhs.ndim - 1]
    dimension_numbers = [(lhs_cont, rhs_cont), (batch_dims, batch_dims)]
    result = lax.dot_general(lhs, rhs, dimension_numbers)
    return lax.reshape(result, result_shape)


def _movechars(s, src, dst):
  """Helper for einsum string munging, like moveaxis on identifier strings."""
  chars = [c for i, c in enumerate(s) if i not in src]
  for i, j in sorted(zip(dst, src)):
    chars.insert(i, s[j])
  return ''.join(chars)


@_wraps(onp.inner)
def inner(a, b):
  if ndim(a) == 0 or ndim(b) == 0:
    return a * b
  return tensordot(a, b, (-1, -1))


@_wraps(onp.outer)
def outer(a, b, out=None):
  if out:
    raise NotImplementedError("The 'out' argument to outer is not supported.")
  return ravel(a)[:, None] * ravel(b)

@_wraps(onp.cross)
def cross(a, b, axisa=-1, axisb=-1, axisc=-1, axis=None):
    if axis is not None:
        axisa = axis
        axisb = axis
        axisc = axis

    a_ndims = len(shape(a))
    b_ndims = len(shape(b))
    axisa = _canonicalize_axis(axisa, a_ndims)
    axisb = _canonicalize_axis(axisb, b_ndims)
    a = moveaxis(a, axisa, -1)
    b = moveaxis(b, axisb, -1)
    a_shape = shape(a)
    b_shape = shape(b)

    if a_shape[-1] not in (2, 3) or b_shape[-1] not in (2, 3):
        raise ValueError("Dimension must be either 2 or 3 for cross product")

    if a_shape[-1] == 2 and b_shape[-1] == 2:
        return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]

    if a_shape[-1] == 2:
        a = concatenate((a, zeros(a_shape[:-1] + (1,), dtype=a.dtype)), axis=-1)
    elif b_shape[-1] == 2:
        b = concatenate((b, zeros(b_shape[:-1] + (1,), dtype=b.dtype)), axis=-1)

    a0 = a[..., 0]
    a1 = a[..., 1]
    a2 = a[..., 2]
    b0 = b[..., 0]
    b1 = b[..., 1]
    b2 = b[..., 2]

    c = array([a1 * b2 - a2 * b1,
               a2 * b0 - a0 * b2,
               a0 * b1 - a1 * b0])

    c_ndims = len(shape(c))
    axisc = _canonicalize_axis(axisc, c_ndims)

    return moveaxis(c, 0, axisc)

@_wraps(onp.kron)
def kron(a, b):
  a, b = _promote_dtypes(a, b)
  if ndim(a) < ndim(b):
    a = reshape(a, (1,) * (ndim(b) - ndim(a)) + shape(a))
  elif ndim(b) < ndim(a):
    b = reshape(b, (1,) * (ndim(a) - ndim(b)) + shape(b))
  a_reshaped = reshape(a, [i for d in shape(a) for i in (d, 1)])
  b_reshaped = reshape(b, [i for d in shape(b) for i in (1, d)])
  out_shape = tuple(onp.multiply(shape(a), shape(b)))
  return reshape(lax.mul(a_reshaped, b_reshaped), out_shape)


@_wraps(onp.vander)
def vander(x, N=None, increasing=False):
  x = asarray(x)
  dtype = _dtype(x)
  if ndim(x) != 1:
    raise ValueError("x must be a one-dimensional array")
  x_shape = shape(x)
  N = N or x_shape[0]
  if N < 0:
    raise ValueError("N must be nonnegative")

  iota = lax.iota(dtype, N)
  if not increasing:
    iota = lax.sub(lax._const(iota, N - 1), iota)

  return power(x[..., None], iota)


### Misc


@_wraps(onp.argmax)
def argmax(a, axis=None):
  if axis is None:
    a = ravel(a)
    axis = 0
  return _argminmax(max, a, axis)


@_wraps(onp.argmin)
def argmin(a, axis=None):
  if axis is None:
    a = ravel(a)
    axis = 0
  return _argminmax(min, a, axis)


# TODO(mattjj): redo this lowering with a call to variadic lax.reduce
def _argminmax(op, a, axis):
  shape = [1] * a.ndim
  shape[axis] = a.shape[axis]
  idxs = lax.tie_in(a, arange(a.shape[axis])).reshape(shape)
  maxval = onp.iinfo(xla_bridge.canonicalize_dtype(idxs.dtype)).max
  maxval = lax.tie_in(a, maxval)
  mask_idxs = where(lax._eq_meet(a, op(a, axis, keepdims=True)), idxs, maxval)
  return min(mask_idxs, axis)


@_wraps(onp.sort)
def sort(a, axis=-1, kind='quicksort', order=None):
  if kind != 'quicksort':
    warnings.warn("'kind' argument to sort is ignored.")
  if order is not None:
    raise ValueError("'order' argument to sort is not supported.")

  if axis is None:
    return lax.sort(a.ravel(), 0)
  else:
    return lax.sort(a, _canonicalize_axis(axis, ndim(a)))


@_wraps(onp.argsort)
def argsort(a, axis=-1, kind='quicksort', order=None):
  if kind != 'quicksort':
    warnings.warn("'kind' argument to argsort is ignored.")
  if order is not None:
    raise ValueError("'order' argument to argsort is not supported.")

  if axis is None:
    return argsort(a.ravel(), 0)
  else:
    axis = _canonicalize_axis(axis, ndim(a))
    iota = lax.broadcasted_iota(onp.int64, shape(a), axis)
    _, perm = lax.sort_key_val(a, iota, dimension=axis)
    return perm


@_wraps(onp.roll)
def roll(a, shift, axis=None):
  a = asarray(a)
  a_shape = shape(a)
  if axis is None:
    return lax.reshape(roll(ravel(a), shift, axis=0), a_shape)

  a_ndim = len(a_shape)
  shift = asarray(shift)
  axis = onp.asarray(axis)
  b_shape = lax.broadcast_shapes(shift.shape, axis.shape, (1,))
  if len(b_shape) != 1:
    msg = "'shift' and 'axis' arguments to roll must be scalars or 1D arrays"
    raise ValueError(msg)
  if b_shape[0] > a_ndim:
    raise ValueError("More shifts/axes than dimensions of input to roll.")

  for x, i in zip(broadcast_to(shift, b_shape),
                  onp.broadcast_to(axis, b_shape)):
    i = _canonicalize_axis(i, a_ndim)
    x = remainder(x, (a_shape[i] or 1))
    a = lax.concatenate((a, a), i)
    a = lax.dynamic_slice_in_dim(a, a_shape[i] - x, a_shape[i], axis=i)
  return a


@_wraps(onp.take)
def take(a, indices, axis=None, out=None, mode=None):
  if out:
    raise NotImplementedError("The 'out' argument to np.take is not supported.")

  a = asarray(a)
  indices = asarray(indices)

  if axis is None:
    a = ravel(a)
    axis = 0
  axis = _canonicalize_axis(axis, ndim(a))

  if mode == "raise":
    # TODO(phawkins): we have no way to report out of bounds errors yet.
    raise NotImplementedError("The 'raise' mode to np.take is not supported.")
  elif mode == "wrap":
    indices = mod(indices, _constant_like(indices, a.shape[axis]))
  elif mode != "clip" and mode is not None:
    raise ValueError("Invalid mode '{}' for np.take".format(mode))

  index_dims = len(shape(indices))
  slice_sizes = list(shape(a))
  slice_sizes[axis] = 1
  dnums = lax.GatherDimensionNumbers(
    offset_dims=tuple(
      list(range(axis)) +
      list(range(axis + index_dims, len(a.shape) + index_dims - 1))),
    collapsed_slice_dims=(axis,),
    start_index_map=(axis,))
  return lax.gather(a, indices[..., None], dimension_numbers=dnums,
                    slice_sizes=tuple(slice_sizes))


def _normalize_index(index, axis_size):
  """Normalizes an index value in the range [-N, N) to the range [0, N)."""
  return lax.select(
    lax.lt(index, _constant_like(index, 0)),
    lax.add(index, _constant_like(index, axis_size)),
    index)

@partial(jit, static_argnums=(2,))
def _take_along_axis(arr, indices, axis):
  if axis is None:
    if ndim(indices) != 1:
      msg = "take_along_axis indices must be 1D if axis=None, got shape {}"
      raise ValueError(msg.format(shape(indices)))
    return take_along_axis(arr.ravel(), indices, 0)
  rank = ndim(arr)
  if rank != ndim(indices):
    msg = "indices and arr must have the same number of dimensions; {} vs. {}"
    raise ValueError(msg.format(ndim(indices), ndim(arr)))
  axis = _canonicalize_axis(axis, rank)

  arr_shape = list(shape(arr))
  axis_size = arr_shape[axis]
  arr_shape[axis] = 1
  idx_shape = shape(indices)
  out_shape = lax.broadcast_shapes(idx_shape, tuple(arr_shape))

  index_dims = [i for i, idx in enumerate(idx_shape) if i == axis or idx != 1]

  gather_index_shape = tuple(onp.array(out_shape)[index_dims]) + (1,)
  gather_indices = []
  slice_sizes = []
  offset_dims = []
  start_index_map = []
  collapsed_slice_dims = []
  j = 0
  for i in range(rank):
    if i == axis:
      indices = _normalize_index(indices, axis_size)
      gather_indices.append(lax.reshape(indices, gather_index_shape))
      slice_sizes.append(1)
      start_index_map.append(i)
      collapsed_slice_dims.append(i)
      j += 1
    elif idx_shape[i] != 1:
      iota = lax.iota(_dtype(indices), out_shape[i])
      iota = lax.tie_in(arr, iota)
      iota = lax.broadcast_in_dim(iota, gather_index_shape, (j,))
      gather_indices.append(iota)
      slice_sizes.append(1)
      start_index_map.append(i)
      collapsed_slice_dims.append(i)
      j += 1
    else:
      # If idx_shape[i] == 1, we can just take the entirety of the arr's axis
      # and avoid forming an iota index.
      offset_dims.append(i)
      slice_sizes.append(arr_shape[i])

  gather_indices = lax.concatenate(gather_indices, dimension=j)
  dnums = lax.GatherDimensionNumbers(
    offset_dims=tuple(offset_dims),
    collapsed_slice_dims=tuple(collapsed_slice_dims),
    start_index_map=tuple(start_index_map))
  return lax.gather(arr, gather_indices, dnums, tuple(slice_sizes))


@_wraps(getattr(onp, "take_along_axis", None))
def take_along_axis(arr, indices, axis):
  return _take_along_axis(arr, indices, axis)

### Indexing

def _rewriting_take(arr, idx):
  # Computes arr[idx].
  # All supported cases of indexing can be implemented as an XLA gather,
  # followed by an optional reverse and a reshape.
  arr = asarray(arr)
  treedef, static_idx, dynamic_idx = _split_index_for_jit(idx)
  return _gather(arr, treedef, static_idx, dynamic_idx)

# TODO(phawkins): re-enable jit after fixing excessive recompilation for
# slice indexes (e.g., slice(0, 5, None), slice(10, 15, None), etc.).
# @partial(jit, static_argnums=(1, 2))
def _gather(arr, treedef, static_idx, dynamic_idx):
  idx = _merge_static_and_dynamic_indices(treedef, static_idx, dynamic_idx)
  indexer = _index_to_gather(shape(arr), idx)  # shared with _scatter_update

  y = lax.gather(arr, indexer.gather_indices, indexer.dnums,
                 indexer.gather_slice_shape)

  # Reverses axes with negative strides.
  if indexer.reversed_y_dims:
    y = lax.rev(y, indexer.reversed_y_dims)

  # This adds np.newaxis/None dimensions.
  return lax.reshape(y, indexer.slice_shape)

_Indexer = collections.namedtuple("_Indexer", [
  # The expected shape of the slice output.
  "slice_shape",

  # The slice shape to pass to lax.gather().
  "gather_slice_shape",

  # The gather indices to use.
  "gather_indices",

  # A GatherDimensionNumbers object describing the gather to perform.
  "dnums",

  # Slice dimensions that have negative strides, and so must be reversed after
  # the gather.
  "reversed_y_dims",

  # For scatters, we must eliminate any axes created by `newaxis`, which
  # are the following dimensions, which must be of size 1. For gathers, we
  # simply reshape to `slice_shape` to introduce the new axes.
  "newaxis_dims",
])

def _split_index_for_jit(idx):
  """Splits indices into necessarily-static and dynamic parts.

  Used to pass indices into `jit`-ted function.
  """
  # Convert list indices to tuples in cases (deprecated by NumPy.)
  idx = _eliminate_deprecated_list_indexing(idx)

  # Expand any (concrete) boolean indices. We can then use advanced integer
  # indexing logic to handle them.
  idx = _expand_bool_indices(idx)

  leaves, treedef = pytree.flatten(idx)
  dynamic = [None] * len(leaves)
  static = [None] * len(leaves)
  for i, x in enumerate(leaves):
    if x is Ellipsis:
      static[i] = x
    elif isinstance(x, slice):
      # slice objects aren't hashable.
      static[i] = (x.start, x.stop, x.step)
    else:
      dynamic[i] = x
  return treedef, tuple(static), dynamic

def _merge_static_and_dynamic_indices(treedef, static_idx, dynamic_idx):
  """Recombines indices that were split by _split_index_for_jit."""
  idx = []
  for s, d in zip(static_idx, dynamic_idx):
    if d is not None:
      idx.append(d)
    elif isinstance(s, tuple):
      idx.append(slice(s[0], s[1], s[2]))
    else:
      idx.append(s)
  return treedef.unflatten(idx)

def _int(aval):
  return not aval.shape and onp.issubdtype(aval.dtype, onp.integer)

def _index_to_gather(x_shape, idx):
  # Remove ellipses and add trailing slice(None)s.
  idx = _canonicalize_tuple_index(len(x_shape), idx)

  # Check for advanced indexing:
  # https://docs.scipy.org/doc/numpy/reference/arrays.indexing.html#advanced-indexing

  # Do the advanced indexing axes appear contiguously? If not, NumPy semantics
  # move the advanced axes to the front.
  advanced_axes_are_contiguous = False

  advanced_indexes = None

  # The positions of the advanced indexing axes in `idx`.
  idx_advanced_axes = []

  # The positions of the advanced indexes in x's shape.
  # collapsed, after None axes have been removed. See below.
  x_advanced_axes = None

  if _is_advanced_int_indexer(idx):
    idx_no_nones = [(i, d) for i, d in enumerate(idx) if d is not None]
    advanced_pairs = (
      (asarray(e), i, j) for j, (i, e) in enumerate(idx_no_nones)
      if (isinstance(e, collections.Sequence) or isinstance(e, ndarray)))
    advanced_pairs = ((_normalize_index(e, x_shape[j]), i, j)
                      for e, i, j in advanced_pairs)
    advanced_indexes, idx_advanced_axes, x_advanced_axes = zip(*advanced_pairs)
    advanced_axes_are_contiguous = onp.all(onp.diff(idx_advanced_axes) == 1)

  x_axis = 0  # Current axis in x.
  y_axis = 0  # Current axis in y, before collapsing. See below.
  collapsed_y_axis = 0  # Current axis in y, after collapsing.

  # Scatter dimension numbers.
  offset_dims = []
  collapsed_slice_dims = []
  start_index_map = []

  gather_indices = zeros((0,), dtype=int32)

  # We perform three transformations to y before the scatter op, in order:
  # First, y is broadcast to slice_shape. In general `y` only need broadcast to
  # the right shape.
  slice_shape = []

  # Next, y is squeezed to remove newaxis_dims. This removes np.newaxis/`None`
  # indices, which the scatter cannot remove itself.
  newaxis_dims = []

  # Finally, we reverse reversed_y_dims to handle slices with negative strides.
  reversed_y_dims = []

  gather_slice_shape = []

  for idx_pos, i in enumerate(idx):
    # Handle the advanced indices here if:
    # * the advanced indices were not contiguous and we are the start.
    # * we are at the position of the first advanced index.
    if (advanced_indexes is not None and
        (advanced_axes_are_contiguous and idx_pos == idx_advanced_axes[0] or
         not advanced_axes_are_contiguous and idx_pos == 0)):
      advanced_indexes = broadcast_arrays(*advanced_indexes)
      shape = advanced_indexes[0].shape
      ndim = len(shape)
      advanced_indexes = [
        lax.convert_element_type(lax.reshape(a, shape + (1,)), int32)
        for a in advanced_indexes]

      # Broadcast gather_indices from [..., k] to [..., 1, 1, ..., 1, k].
      gather_indices = lax.broadcast_in_dim(
        gather_indices, onp.insert(gather_indices.shape, -1, shape),
        tuple(range(gather_indices.ndim - 1)) + (gather_indices.ndim + ndim - 1,))
      gather_indices = concatenate([gather_indices] + advanced_indexes, -1)
      start_index_map.extend(x_advanced_axes)
      collapsed_slice_dims.extend(x_advanced_axes)
      slice_shape.extend(shape)
      y_axis += ndim
      collapsed_y_axis += ndim

    # Per-index bookkeeping for advanced indexes.
    if idx_pos in idx_advanced_axes:
      x_axis += 1
      gather_slice_shape.append(1)
      continue

    try:
      abstract_i = core.get_aval(i)
    except TypeError:
      abstract_i = None
    # Handle basic int indexes.
    if (isinstance(abstract_i, ConcreteArray) or
        isinstance(abstract_i, ShapedArray)) and _int(abstract_i):
      i = _normalize_index(i, x_shape[x_axis])
      i = lax.convert_element_type(i, int32)
      i = broadcast_to(i, tuple(gather_indices.shape[:-1]) + (1,))
      gather_indices = concatenate((gather_indices, i), -1)
      collapsed_slice_dims.append(x_axis)
      gather_slice_shape.append(1)
      start_index_map.append(x_axis)
      x_axis += 1
    # Handle np.newaxis (None)
    elif i is None:
      slice_shape.append(1)
      newaxis_dims.append(y_axis)
      y_axis += 1
    # Handle slice(None)
    elif _is_slice_none(i):
      slice_shape.append(x_shape[x_axis])
      gather_slice_shape.append(x_shape[x_axis])
      offset_dims.append(collapsed_y_axis)
      collapsed_y_axis += 1
      y_axis += 1
      x_axis += 1
    # Handle slice index (only static, otherwise an error is raised)
    elif isinstance(i, slice):
      if not _all(elt is None or type(core.get_aval(elt)) is ConcreteArray
                  for elt in (i.start, i.stop, i.step)):
        msg = ("Array slice indices must have static start/stop/step to be used "
               "with Numpy indexing syntax. Try lax.dynamic_slice/"
               "dynamic_update_slice instead.")
        raise IndexError(msg)
      start, limit, stride, needs_rev = _static_idx(i, x_shape[x_axis])
      if needs_rev:
        reversed_y_dims.append(collapsed_y_axis)
      if stride == 1:
        i = lax.convert_element_type(start, int32)
        i = broadcast_to(i, tuple(gather_indices.shape[:-1]) + (1,))
        gather_indices = concatenate((gather_indices, i), -1)
        slice_shape.append(limit - start)
        gather_slice_shape.append(limit - start)
        offset_dims.append(collapsed_y_axis)
        start_index_map.append(x_axis)
      else:
        i = arange(start, limit, stride, dtype=int32)
        size = i.shape[0]
        slice_shape.append(size)
        gather_slice_shape.append(1)
        gather_indices_shape = tuple(gather_indices.shape[:-1]) + (size,)
        i = lax.broadcast_in_dim(
            i, shape=gather_indices_shape + (1,),
            broadcast_dimensions=(len(gather_indices_shape) - 1,))
        gather_indices = lax.broadcast_in_dim(
            gather_indices,
            shape=gather_indices_shape + (len(start_index_map),),
            broadcast_dimensions=(
              tuple(range(len(gather_indices_shape) - 1)) +
              (len(gather_indices_shape),)))
        gather_indices = concatenate(
          (gather_indices, i), len(gather_indices_shape))
        start_index_map.append(x_axis)
        collapsed_slice_dims.append(x_axis)

      collapsed_y_axis += 1
      y_axis += 1
      x_axis += 1
    else:
      msg = "Indexing mode not yet supported. Open a feature request!\n{}"
      raise IndexError(msg.format(idx))

  dnums = lax.GatherDimensionNumbers(
    offset_dims = tuple(offset_dims),
    collapsed_slice_dims = tuple(sorted(collapsed_slice_dims)),
    start_index_map = tuple(start_index_map)
  )
  return _Indexer(
    slice_shape=slice_shape,
    newaxis_dims=tuple(newaxis_dims),
    gather_slice_shape=gather_slice_shape,
    reversed_y_dims=reversed_y_dims,
    dnums=dnums,
    gather_indices=gather_indices)

def _should_unpack_list_index(x):
  """Helper for _eliminate_deprecated_list_indexing."""
  return (isinstance(x, ndarray) and onp.ndim(x) != 0
          or isinstance(x, collections.Sequence)
          or isinstance(x, slice) or x is Ellipsis or x is None)

def _eliminate_deprecated_list_indexing(idx):
  # "Basic slicing is initiated if the selection object is a non-array,
  # non-tuple sequence containing slice objects, [Ellipses, or newaxis
  # objects]". Detects this case and canonicalizes to a tuple. This case is
  # deprecated by NumPy and exists for backward compatibility.
  if not isinstance(idx, tuple):
    if isinstance(idx, collections.Sequence) and not isinstance(idx, ndarray):
      if _any(_should_unpack_list_index(i) for i in idx):
        idx = tuple(idx)
      else:
        idx = (idx,)
    else:
      idx = (idx,)
  return idx

def _expand_bool_indices(idx):
  """Converts concrete bool indexes into advanced integer indexes."""
  out = []
  for i in idx:
    try:
      abstract_i = core.get_aval(i)
    except TypeError:
      abstract_i = None
    if (isinstance(abstract_i, ShapedArray) and onp.issubdtype(abstract_i.dtype, onp.bool_)
          or isinstance(i, list) and _all(not _shape(e) and onp.issubdtype(_dtype(e), onp.bool_)
                                          for e in i)):
      if isinstance(i, list):
        i = array(i)
        abstract_i = core.get_aval(i)

      if not type(abstract_i) is ConcreteArray:
        msg = ("Array boolean indices must be static (e.g. no dependence on an "
               "argument to a jit or vmap function).")
        raise IndexError(msg)
      else:
        out.extend(onp.where(i))
    else:
      out.append(i)
  return tuple(out)

def _is_slice_none(idx):
  """Return True if idx is equal to slice(None), False otherwise."""
  if isinstance(idx, slice):
    return idx.start is None and idx.stop is None and idx.step is None

# TODO(mattjj): clean up this logic
def _is_advanced_int_indexer(idx):
  """Returns True if idx should trigger int array indexing, False otherwise."""
  # https://docs.scipy.org/doc/numpy/reference/arrays.indexing.html#advanced-indexing
  assert isinstance(idx, tuple)
  if _all(onp.ndim(elt) == 0 for elt in idx):
    return False
  return _all(e is None or e is Ellipsis or isinstance(e, slice)
              or _is_int_arraylike(e) for e in idx)

def _is_int_arraylike(x):
  """Returns True if x is array-like with integer dtype, False otherwise."""
  return (isinstance(x, int) and not isinstance(x, bool)
          or onp.issubdtype(getattr(x, "dtype", None), onp.integer)
          or isinstance(x, (list, tuple)) and _all(_is_int_arraylike(e) for e in x))


def _canonicalize_tuple_index(arr_ndim, idx):
  """Helper to remove Ellipsis and add in the implicit trailing slice(None)."""
  len_without_none = _sum(1 for e in idx if e is not None and e is not Ellipsis)
  if len_without_none > arr_ndim:
    msg = "Too many indices for array: {} non-None/Ellipsis indices for dim {}."
    raise IndexError(msg.format(len_without_none, arr_ndim))
  ellipses = (i for i, elt in enumerate(idx) if elt is Ellipsis)
  ellipsis_index = next(ellipses, None)
  if ellipsis_index is not None:
    if next(ellipses, None) is not None:
      msg = "Multiple ellipses (...) not supported: {}."
      raise IndexError(msg.format(list(map(type, idx))))
    colons = (slice(None),) * (arr_ndim - len_without_none)
    idx = idx[:ellipsis_index] + colons + idx[ellipsis_index + 1:]
  elif len_without_none < arr_ndim:
    colons = (slice(None),) * (arr_ndim - len_without_none)
    idx = tuple(idx) + colons
  return idx


def _static_idx(idx, size):
  """Helper function to compute the static slice start/limit/stride values."""
  assert isinstance(idx, slice)
  start, stop, step = idx.indices(size)
  if (step < 0 and stop >= start) or (step > 0 and start >= stop):
    return 0, 0, 1, False  # sliced to size zero

  if step > 0:
    return start, stop, step, False
  else:
    k  = (start - stop - 1) % (-step)
    return stop + k + 1, start + 1, -step, True


blackman = _wrap_numpy_nullary_function(onp.blackman)
bartlett = _wrap_numpy_nullary_function(onp.bartlett)
hamming = _wrap_numpy_nullary_function(onp.hamming)
hanning = _wrap_numpy_nullary_function(onp.hanning)
# TODO: lower `kaiser` via lax to allow non-constant beta values.
kaiser = _wrap_numpy_nullary_function(onp.kaiser)


@_wraps(getattr(onp, "gcd", None))
def gcd(x1, x2):
  if (not issubdtype(_dtype(x1), integer) or
      not issubdtype(_dtype(x2), integer)):
    raise ValueError("Arguments to gcd must be integers.")
  def cond_fn(xs):
    x1, x2 = xs
    return any(x2 != 0)
  def body_fn(xs):
    x1, x2 = xs
    x1, x2 = (where(x2 != 0, x2, x1),
              where(x2 != 0, lax.rem(x1, x2), lax._const(x2, 0)))
    return (where(x1 < x2, x2, x1), where(x1 < x2, x1, x2))
  x1, x2 = _promote_dtypes(lax.abs(x1), lax.abs(x2))
  x1, x2 = broadcast_arrays(x1, x2)
  gcd, _ = lax.while_loop(cond_fn, body_fn, (x1, x2))
  return gcd


@_wraps(getattr(onp, "lcm", None))
def lcm(x1, x2):
  d = gcd(x1, x2)
  return where(d == 0, lax._const(d, 0),
               lax.div(lax.abs(multiply(x1, x2)), d))

@_wraps(onp.cov)
def cov(m, y=None, rowvar=True, bias=False, ddof=None, fweights=None,
        aweights=None):
  msg = ("jax.numpy.cov not implemented for nontrivial {}. "
         "Open a feature request at https://github.com/google/jax/issues !")
  if y is not None: raise NotImplementedError(msg.format('y'))
  # These next two are actually implemented, just not tested.
  if fweights is not None: raise NotImplementedError(msg.format('fweights'))
  if aweights is not None: raise NotImplementedError(msg.format('aweights'))

  if m.ndim > 2:
    raise ValueError("m has more than 2 dimensions")  # same as numpy error
  X = array(m, ndmin=2, dtype=xla_bridge.canonicalize_dtype(result_type(m, onp.float64)), copy=False)
  if not rowvar and X.shape[0] != 1:
    X = X.T
  if X.shape[0] == 0:
    return onp.array([]).reshape(0, 0)
  if ddof is None:
    ddof = 1 if bias == 0 else 0

  w = None
  if fweights is not None:
    if onp.ndim(fweights) > 1:
      raise RuntimeError("cannot handle multidimensional fweights")
    if onp.shape(fweights)[0] != X.shape[1]:
      raise RuntimeError("incompatible numbers of samples and fweights")
    w = asarray(fweights)
  if aweights is not None:
    if onp.ndim(aweights) > 1:
      raise RuntimeError("cannot handle multidimensional aweights")
    if onp.shape(aweights)[0] != X.shape[1]:
      raise RuntimeError("incompatible numbers of samples and aweights")
    w = aweights if w is None else w * aweights

  avg, w_sum = average(X, axis=1, weights=w, returned=True)
  w_sum = w_sum[0]

  if w is None:
    f = X.shape[1] - ddof
  elif ddof == 0:
    f = w_sum
  elif aweights is None:
    f = w_sum - ddof
  else:
    f = w_sum - ddof * sum(w * aweights) / w_sum

  X = X - avg[:, None]
  X_T = X.T if w is None else (X * w).T
  return true_divide(dot(X, X_T.conj()), f).squeeze()


@_wraps(onp.corrcoef)
def corrcoef(x, y=None, rowvar=True, bias=None, ddof=None):
  c = cov(x, y, rowvar)
  if len(shape(c)) == 0:
      # scalar - this should yield nan for values (nan/nan, inf/inf, 0/0), 1 otherwise
      return divide(c, c)
  d = diag(c)
  stddev = sqrt(real(d))
  c = divide(c, stddev[:,None])
  c = divide(c, stddev[None,:])

  real_part = clip(real(c), -1, 1)
  if iscomplexobj(c):
      complex_part = clip(imag(c), -1, 1)
      c = lax.complex(real_part, complex_part)
  else:
      c = real_part
  return c

@_wraps(getattr(onp, "quantile", None))
def quantile(a, q, axis=None, out=None, overwrite_input=False,
             interpolation="linear", keepdims=False):
  if overwrite_input or out is not None:
    msg = ("jax.numpy.quantile does not support overwrite_input=True or "
           "out != None")
    raise ValueError(msg)
  if interpolation != "linear":
    raise NotImplementedError("Only interpolation='linear' is implemented")

  a = asarray(a)
  q = asarray(q)

  if axis is None:
    a = ravel(a)
    axis = 0
  elif isinstance(axis, tuple):
    raise NotImplementedError("Tuple values for axis are not implemented")
  else:
    axis = _canonicalize_axis(axis, ndim(a))

  q_ndim = ndim(q)
  if q_ndim > 1:
    raise ValueError("q must be have rank <= 1, got shape {}".format(shape(q)))

  a, q = _promote_dtypes(a, q)
  if not issubdtype(a.dtype, floating):
    msg = "q and a arguments to quantile must be of float type, got {} and {}"
    raise TypeError(msg.format(a.dtype, q.dtype))

  a_shape = shape(a)
  a = lax.sort(a, dimension=axis)

  n = a_shape[axis]
  q = lax.mul(q, _constant_like(q, n - 1))
  low = lax.floor(q)
  high = lax.add(low, _constant_like(low, 1))
  high_weight = lax.sub(q, low)
  low_weight = lax.sub(_constant_like(high_weight, 1), high_weight)

  low = lax.clamp(_constant_like(low, 0), low, _constant_like(low, n - 1))
  high = lax.clamp(_constant_like(high, 0), high, _constant_like(high, n - 1))
  low = lax.convert_element_type(low, int64)
  high = lax.convert_element_type(high, int64)

  slice_sizes = list(a_shape)
  slice_sizes[axis] = 1

  dnums = lax.GatherDimensionNumbers(
    offset_dims=tuple(range(
      q_ndim,
      len(a_shape) + q_ndim if keepdims else len(a_shape) + q_ndim - 1)),
    collapsed_slice_dims=() if keepdims else (axis,),
    start_index_map=(axis,))
  low = low[..., None]
  high = high[..., None]
  low_value = lax.gather(a, low, dimension_numbers=dnums,
                         slice_sizes=slice_sizes)
  high_value = lax.gather(a, high, dimension_numbers=dnums,
                          slice_sizes=slice_sizes)
  if q_ndim == 1:
    low_weight = lax.broadcast_in_dim(low_weight, low_value.shape,
                                      broadcast_dimensions=(0,))
    high_weight = lax.broadcast_in_dim(high_weight, high_value.shape,
                                      broadcast_dimensions=(0,))
  return lax.add(lax.mul(low_value, low_weight),
                 lax.mul(high_value, high_weight))


@_wraps(onp.percentile)
def percentile(a, q, axis=None, out=None, overwrite_input=False,
               interpolation="linear", keepdims=False):
  q = true_divide(asarray(q), float32(100.0))
  return quantile(a, q, axis=axis, out=out, overwrite_input=overwrite_input,
                  interpolation=interpolation, keepdims=keepdims)


@_wraps(onp.median)
def median(a, axis=None, out=None, overwrite_input=False, keepdims=False):
    q = 0.5
    return quantile(a, q, axis=axis, out=out, overwrite_input=overwrite_input,
                    keepdims=keepdims)

def _astype(arr, dtype):
  lax._check_user_dtype_supported(dtype, "astype")
  return lax.convert_element_type(arr, dtype)

### track unimplemented functions

def _not_implemented(fun):
  @_wraps(fun)
  def wrapped(*args, **kwargs):
    msg = "Numpy function {} not yet implemented"
    raise NotImplementedError(msg.format(fun))
  return wrapped

# Build a set of all unimplemented NumPy functions.
for func in get_module_functions(onp):
  if func.__name__ not in globals():
    globals()[func.__name__] = _not_implemented(func)


### add method and operator overloads to arraylike classes

# We add operator overloads to DeviceArray and ShapedArray. These method and
# operator overloads mainly just forward calls to the corresponding lax_numpy
# functions, which can themselves handle instances from any of these classes.


def _swap_args(f):
  return lambda x, y: f(y, x)

def _unimplemented_setitem(self, i, x):
  msg = ("'{}' object does not support item assignment. JAX arrays are "
         "immutable; perhaps you want jax.ops.index_update or "
         "jax.ops.index_add instead?")
  raise TypeError(msg.format(type(self)))

_operators = {
    "getitem": _rewriting_take,
    "setitem": _unimplemented_setitem,
    "neg": negative,
    "eq": equal,
    "ne": not_equal,
    "lt": less,
    "le": less_equal,
    "gt": greater,
    "ge": greater_equal,
    "abs": abs,
    "add": add,
    "radd": add,
    "sub": subtract,
    "rsub": _swap_args(subtract),
    "mul": multiply,
    "rmul": multiply,
    "div": divide,
    "rdiv": _swap_args(divide),
    "truediv": true_divide,
    "rtruediv": _swap_args(true_divide),
    "floordiv": floor_divide,
    "rfloordiv": _swap_args(floor_divide),
    "divmod": divmod,
    "rdivmod": _swap_args(divmod),
    "mod": mod,
    "rmod": _swap_args(mod),
    "pow": power,
    "rpow": _swap_args(power),
    "matmul": matmul,
    "rmatmul": _swap_args(matmul),
    "and": bitwise_and,
    "rand": bitwise_and,
    "or": bitwise_or,
    "ror": bitwise_or,
    "xor": bitwise_xor,
    "rxor": bitwise_xor,
    "invert": bitwise_not,
    "lshift": left_shift,
    "rshift": right_shift,
}

# These numpy.ndarray methods are just refs to an equivalent numpy function
_nondiff_methods = ["all", "any", "argmax", "argmin", "argpartition", "argsort",
                    "nonzero", "searchsorted", "round"]
_diff_methods = ["clip", "compress", "conj", "conjugate", "cumprod", "cumsum",
                 "diagonal", "dot", "max", "mean", "min", "prod", "ptp",
                 "ravel", "repeat", "sort", "squeeze", "std", "sum",
                 "swapaxes", "take", "tile", "trace", "transpose", "var"]


# Set up operator, method, and property forwarding on Tracer instances containing
# ShapedArray avals by following the forwarding conventions for Tracer.
# Forward operators using a single-underscore-prefix naming convention:
for operator_name, function in _operators.items():
  setattr(ShapedArray, "_{}".format(operator_name), staticmethod(function))
# Forward methods and properties using core.aval_method and core.aval_property:
for method_name in _nondiff_methods + _diff_methods:
  setattr(ShapedArray, method_name, core.aval_method(globals()[method_name]))
setattr(ShapedArray, "reshape", core.aval_method(_reshape_method))
setattr(ShapedArray, "flatten", core.aval_method(ravel))
setattr(ShapedArray, "T", core.aval_property(transpose))
setattr(ShapedArray, "real", core.aval_property(real))
setattr(ShapedArray, "imag", core.aval_property(imag))
setattr(ShapedArray, "astype", core.aval_method(_astype))


# Forward operators, methods, and properties on DeviceArray to lax_numpy
# functions (with no Tracers involved; this forwarding is direct)
for operator_name, function in _operators.items():
  setattr(DeviceArray, "__{}__".format(operator_name), function)
for method_name in _nondiff_methods + _diff_methods:
  setattr(DeviceArray, method_name, globals()[method_name])
setattr(DeviceArray, "reshape", _reshape_method)
setattr(DeviceArray, "flatten", ravel)
setattr(DeviceArray, "T", property(transpose))
setattr(DeviceArray, "real", property(real))
setattr(DeviceArray, "imag", property(imag))
setattr(DeviceArray, "astype", _astype)


# Extra methods that are handy
setattr(ShapedArray, "broadcast", core.aval_method(lax.broadcast))
setattr(ShapedArray, "broadcast_in_dim", core.aval_method(lax.broadcast_in_dim))
setattr(ShapedArray, "split", core.aval_method(split))
setattr(DeviceArray, "broadcast", lax.broadcast)
setattr(DeviceArray, "broadcast_in_dim", lax.broadcast_in_dim)
setattr(DeviceArray, "split", split)

@jit
def _unstack(x):
  if x.ndim == 0:
    raise ValueError("Argument to _unstack must be non-scalar")
  return [lax.index_in_dim(x, i, keepdims=False) for i in range(x.shape[0])]
setattr(DeviceArray, "_unstack", _unstack)
