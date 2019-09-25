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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import enum
import itertools
import operator
import string
import warnings

import six
from six.moves import builtins, xrange

import numpy as onp

from ..util import partial, prod

from .. import core
from .. import ad_util
from .. import api
from .. import linear_util as lu
from ..config import flags
from ..core import Primitive
from ..abstract_arrays import (UnshapedArray, ShapedArray, ConcreteArray,
                               array_types, make_shaped_array, raise_to_shaped)
from ..interpreters import partial_eval as pe
from ..interpreters import xla
from ..interpreters import pxla
from ..interpreters import ad
from ..interpreters import batching
from ..interpreters import masking
from ..interpreters.masking import ShapeExpr, ShapeError
from ..util import curry, cache, safe_zip, unzip2, prod
from ..tree_util import build_tree, tree_unflatten, tree_map
from ..lib import xla_bridge
from ..lib import xla_client

FLAGS = flags.FLAGS

_max = builtins.max
_min = builtins.max
_reduce = six.moves.reduce


@cache()
def broadcast_shapes(*shapes):
  """Returns the shape that results from NumPy broadcasting of `shapes`."""
  if len(shapes) == 1:
    return shapes[0]
  ndim = _max(len(shape) for shape in shapes)
  shapes = onp.array([(1,) * (ndim - len(shape)) + shape for shape in shapes])
  min_shape = onp.min(shapes, axis=0)
  max_shape = onp.max(shapes, axis=0)
  result_shape = onp.where(min_shape == 0, 0, max_shape)
  if not onp.all((shapes == result_shape) | (shapes == 1)):
    raise ValueError("Incompatible shapes for broadcasting: {}"
                     .format(tuple(map(tuple, shapes))))
  return tuple(result_shape)

def _canonicalize_shape(shape):
  """Canonicalizes and checks for errors in a user-provided shape value.

  Args:
    shape: a Python value that represents a shape.

  Returns:
    A tuple of integers.
  """
  # TODO(mattjj): this next check is a temporary workaround for masking
  if (type(shape) is ShapeExpr
      or type(shape) is tuple and any(type(d) is masking.Poly for d in shape)):
    return shape
  try:
    return tuple(map(operator.index, shape))
  except TypeError:
    pass
  msg = ("Shapes must be 1D sequences of concrete values of integer type, "
         "got {}")
  raise TypeError(msg.format(shape))

def _identity(x): return x

### traceables

def neg(x):
  r"""Elementwise negation: :math:`-x`."""
  return neg_p.bind(x)

def sign(x):
  r"""Elementwise sign.

  :math:`\mathrm{sign}(x) = \begin{cases}
  -1 & x < 0\\
  -0 & x = -0\\
  \mathit{NaN} & x = \mathit{NaN}\\
  +0 & x = +0\\
  1 & x > 0
  \end{cases}`.
  """
  return sign_p.bind(x)

def floor(x):
  r"""Elementwise floor: :math:`\left\lfloor x \right\rfloor`."""
  return floor_p.bind(x)

def ceil(x):
  r"""Elementwise ceiling: :math:`\left\lceil x \right\rceil`."""
  return ceil_p.bind(x)

def round(x):
  r"""Elementwise round.

  Rounds values to the nearest integer. Halfway values (e.g., `0.5`) are rounded
  away from zero."""
  return round_p.bind(x)

def is_finite(x):
  r"""Elementwise :math:`\mathrm{isfinite}`.

  For each element x returns `True` if and only if x is not :math:`\pm\infty` or
  :math:`\mathit{NaN}`.
  """
  return is_finite_p.bind(x)

def exp(x):
  r"""Elementwise exponential: :math:`e^x`."""
  return exp_p.bind(x)

def expm1(x):
  r"""Elementwise :math:`e^{x - 1}`."""
  return expm1_p.bind(x)

def log(x):
  r"""Elementwise natural logarithm: :math:`\mathrm{log}(x)`."""
  return log_p.bind(x)

def log1p(x):
  r"""Elementwise :math:`\mathrm{log}(1 + x)`."""
  return log1p_p.bind(x)

def tanh(x):
  r"""Elementwise hyperbolic tangent: :math:`\mathrm{tanh}(x)`."""
  return tanh_p.bind(x)

def sin(x):
  r"""Elementwise sine: :math:`\mathrm{sin}(x)`."""
  return sin_p.bind(x)

def cos(x):
  r"""Elementwise cosine: :math:`\mathrm{cos}(x)`."""
  return cos_p.bind(x)

def atan2(x, y):
  r"""Elementwise arc tangent of two variables:
    :math:`\mathrm{atan}({x \over y})`."""
  return atan2_p.bind(x, y)

def lgamma(x):
  r"""Elementwise log gamma: :math:`\mathrm{log}(\Gamma(x))`."""
  return lgamma_p.bind(x)

def digamma(x):
  r"""Elementwise digamma: :math:`\psi(x)`."""
  return digamma_p.bind(x)

def erf(x):
  r"""Elementwise error function: :math:`\mathrm{erf}(x)`."""
  return erf_p.bind(x)

def erfc(x):
  r"""Elementwise complementary error function:
    :math:`\mathrm{erfc}(x) = 1 - \mathrm{erf}(x)`."""
  return erfc_p.bind(x)

def erf_inv(x):
  r"""Elementwise inverse error function: :math:`\mathrm{erf}^{-1}(x)`."""
  return erf_inv_p.bind(x)

def real(x):
  r"""Elementwise extract real part: :math:`\mathrm{Re}(x)`.

  Returns the real part of a complex number.
  """
  return real_p.bind(x)

def imag(x):
  r"""Elementwise extract imaginary part: :math:`\mathrm{Im}(x)`.

  Returns the imaginary part of a complex number.
  """
  return imag_p.bind(x)

def complex(x, y):
  r"""Elementwise make complex number: :math:`x + jy`.

  Builds a complex number from real and imaginary parts.
  """
  return complex_p.bind(_brcast(x, y), _brcast(y, x))

def conj(x):
  r"""Elementwise complex conjugate function: :math:`\overline{x}`."""
  return conj_p.bind(x, input_dtype=_dtype(x))

def abs(x):
  r"""Elementwise absolute value: :math:`|x|`."""
  return abs_p.bind(x)

def pow(x, y):
  r"""Elementwise power: :math:`x^y`."""
  return pow_p.bind(x, y)

def sqrt(x):
  r"""Elementwise square root: :math:`\sqrt{x}`."""
  return sqrt_p.bind(x)

def rsqrt(x):
  r"""Elementwise reciprocal square root:  :math:`1 \over \sqrt{x}."""
  return rsqrt_p.bind(x)

def bitwise_not(x):
  r"""Elementwise NOT: :math:`\neg x`."""
  return not_p.bind(x)

def bitwise_and(x, y):
  r"""Elementwise AND: :math:`x \wedge y`."""
  return and_p.bind(x, y)

def bitwise_or(x, y):
  r"""Elementwise OR: :math:`x \vee y`."""
  return or_p.bind(x, y)

def bitwise_xor(x, y):
  r"""Elementwise exclusive OR: :math:`x \oplus y`."""
  return xor_p.bind(x, y)

def add(x, y):
  r"""Elementwise addition: :math:`x + y`."""
  return add_p.bind(x, y)

def sub(x, y):
  r"""Elementwise subtraction: :math:`x - y`."""
  return sub_p.bind(x, y)

def mul(x, y):
  r"""Elementwise multiplication: :math:`x \times y`."""
  return mul_p.bind(x, y)

def div(x, y):
  r"""Elementwise division: :math:`x \over y`."""
  return div_p.bind(x, y)

def rem(x, y):
  r"""Elementwise remainder: :math:`x \bmod y`."""
  return rem_p.bind(x, y)

def max(x, y):
  r"""Elementwise maximum: :math:`\mathrm{max}(x, y)`

  For complex numbers, uses a lexicographic comparison on the
  `(real, imaginary)` pairs."""
  return max_p.bind(x, y)

def min(x, y):
  r"""Elementwise minimum:  :math:`\mathrm{min}(x, y)`

  For complex numbers, uses a lexicographic comparison on the
  `(real, imaginary)` pairs."""
  return min_p.bind(x, y)

def shift_left(x, y):
  r"""Elementwise left shift: :math:`x \ll y`."""
  return shift_left_p.bind(x, y)

def shift_right_arithmetic(x, y):
  r"""Elementwise arithmetic right shift: :math:`x \gg y`."""
  return shift_right_arithmetic_p.bind(x, y)

def shift_right_logical(x, y):
  r"""Elementwise logical right shift: :math:`x \gg y`."""
  return shift_right_logical_p.bind(x, y)

def eq(x, y):
  r"""Elementwise equals: :math:`x = y`."""
  return eq_p.bind(x, y)

def ne(x, y):
  r"""Elementwise not-equals: :math:`x \neq y`."""
  return ne_p.bind(x, y)

def ge(x, y):
  r"""Elementwise greater-than-or-equals: :math:`x \geq y`."""
  return ge_p.bind(x, y)

def gt(x, y):
  r"""Elementwise greater-than: :math:`x > y`."""
  return gt_p.bind(x, y)

def le(x, y):
  r"""Elementwise less-than-or-equals: :math:`x \leq y`."""
  return le_p.bind(x, y)

def lt(x, y):
  r"""Elementwise less-than: :math:`x < y`."""
  return lt_p.bind(x, y)

def convert_element_type(operand, new_dtype):
  """Elementwise cast.

  Wraps XLA's `ConvertElementType
  <https://www.tensorflow.org/xla/operation_semantics#convertelementtype>`_
  operator, which performs an elementwise conversion from one type to another.
  Similar to a C++ `static_cast`.

  Args:
    operand: an array or scalar value to be cast
    new_dtype: the new type. Should be a NumPy type.

  Returns:
    An array with the same shape as `operand`, cast elementwise to `new_dtype`.
  """
  new_dtype = xla_bridge.canonicalize_dtype(new_dtype)
  old_dtype = _dtype(operand)
  if old_dtype != new_dtype:
    if (onp.issubdtype(old_dtype, onp.complexfloating) and
        not onp.issubdtype(new_dtype, onp.complexfloating)):
      msg = "Casting complex values to real discards the imaginary part"
      warnings.warn(msg, onp.ComplexWarning)
      operand = real(operand)
      old_dtype = _dtype(operand)
    return convert_element_type_p.bind(
        operand, new_dtype=new_dtype, old_dtype=old_dtype)
  else:
    return operand

def bitcast_convert_type(operand, new_dtype):
  """Elementwise bitcast.

  Wraps XLA's `BitcastConvertType
  <https://www.tensorflow.org/xla/operation_semantics#bitcastconverttype>`_
  operator, which performs a bit cast from one type to another. The bitwidth
  of the source and destination types must match.

  Args:
    operand: an array or scalar value to be cast
    new_dtype: the new type. Should be a NumPy type.

  Returns:
    An array with the same shape as `operand`, bitcast elementwise to
    `new_dtype`.
  """
  new_dtype = xla_bridge.canonicalize_dtype(new_dtype)
  old_dtype = _dtype(operand)
  if old_dtype != new_dtype:
    return bitcast_convert_type_p.bind(operand, new_dtype=new_dtype)
  else:
    return operand

def clamp(min, x, max):
  r"""Elementwise clamp.

  Returns :math:`\mathrm{clamp}(x) = \begin{cases}
  \mathit{min} & \text{if } x < \mathit{min},\\
  \mathit{max} & \text{if } x > \mathit{max},\\
  x & \text{otherwise}
  \end{cases}`.
  """
  return clamp_p.bind(min, x, max)

def concatenate(operands, dimension):
  """Concatenates a sequence of arrays along `dimension`.

  Wraps XLA's `Concatenate
  <https://www.tensorflow.org/xla/operation_semantics#concatenate>`_
  operator.

  Args:
    operands: a sequence of arrays to concatenate. The arrays must have equal
      shapes, except in the `dimension` axis.
    dimension: the dimension along which to concatenate the arrays.

  Returns:
    An array containing the concatenation.
  """
  return concatenate_p.bind(*operands, dimension=dimension,
                            operand_shapes=tuple(o.shape for o in operands))

Precision = xla_client.PrecisionConfig.Precision

def conv_general_dilated(lhs, rhs, window_strides, padding, lhs_dilation=None,
                         rhs_dilation=None, dimension_numbers=None,
                         feature_group_count=1, precision=None):
  """General n-dimensional convolution operator, with optional dilation.

  Wraps XLA's `Conv
  <https://www.tensorflow.org/xla/operation_semantics#conv_convolution>`_
  operator.

  Args:
    lhs: a rank `n+2` dimensional input array.
    rhs: a rank `n+2` dimensional array of kernel weights.
    window_strides: a sequence of `n` integers, representing the inter-window
      strides.
    padding: either the string `'SAME'`, the string `'VALID'`, or a sequence of
      `n` `(low, high)` integer pairs that give the padding to apply before and
      after each spatial dimension.
    lhs_dilation: `None`, or a sequence of `n` integers, giving the
      dilation factor to apply in each spatial dimension of `lhs`. LHS dilation
      is also known as transposed convolution.
    rhs_dilation: `None`, or a sequence of `n` integers, giving the
      dilation factor to apply in each spatial dimension of `rhs`. RHS dilation
      is also known as atrous convolution.
    dimension_numbers: either `None`, a `ConvDimensionNumbers` object, or
      a 3-tuple `(lhs_spec, rhs_spec, out_spec)`, where each element is a string
      of length `n+2`.
    feature_group_count: integer, default 1. See XLA HLO docs.
    precision: Optional. Either `None`, which means the default precision for
      the backend, or a `Precision` enum value.

  Returns:
    An array containing the convolution result.

  In the string case of `dimension_numbers`, each character identifies by
  position:

  - the batch dimensions in `lhs`, `rhs`, and the output with the character
    'N',
  - the feature dimensions in `lhs` and the output with the character 'C',
  - the input and output feature dimensions in rhs with the characters 'I'
    and 'O' respectively, and
  - spatial dimension correspondences between lhs, rhs, and the output using
    any distinct characters.

  For example, to indicate dimension numbers consistent with the `conv` function
  with two spatial dimensions, one could use `('NCHW', 'OIHW', 'NCHW')`. As
  another example, to indicate dimension numbers consistent with the TensorFlow
  Conv2D operation, one could use `('NHWC', 'HWIO', 'NHWC')`. When using the
  latter form of convolution dimension specification, window strides are
  associated with spatial dimension character labels according to the order in
  which the labels appear in the `rhs_spec` string, so that `window_strides[0]`
  is matched with the dimension corresponding to the first character
  appearing in rhs_spec that is not `'I'` or `'O'`.

  If `dimension_numbers` is `None`, the default is `('NCHW', 'OIHW', 'NCHW')`
  (for a 2D convolution).
  """
  if type(dimension_numbers) is not ConvDimensionNumbers:
    dimension_numbers = conv_dimension_numbers(
        lhs.shape, rhs.shape, dimension_numbers)
  if isinstance(padding, str):
    lhs_perm, rhs_perm, _ = dimension_numbers
    padding = padtype_to_pads(
        onp.take(lhs.shape, lhs_perm)[2:], onp.take(rhs.shape, rhs_perm)[2:],
        window_strides, padding)
  if lhs_dilation is None:
    lhs_dilation = (1,) * (lhs.ndim - 2)
  if rhs_dilation is None:
    rhs_dilation = (1,) * (rhs.ndim - 2)
  return conv_general_dilated_p.bind(
      lhs, rhs, window_strides=tuple(window_strides), padding=tuple(padding),
      lhs_dilation=tuple(lhs_dilation), rhs_dilation=tuple(rhs_dilation),
      dimension_numbers=dimension_numbers,
      feature_group_count=feature_group_count,
      lhs_shape=lhs.shape, rhs_shape=rhs.shape,
      precision=_canonicalize_precision(precision))

def dot(lhs, rhs, precision=None):
  """Vector/vector, matrix/vector, and matrix/matrix multiplication.

  Wraps XLA's `Dot
  <https://www.tensorflow.org/xla/operation_semantics#dot>`_
  operator.

  For more general contraction, see the `dot_general` operator.

  Args:
    lhs: an array of rank 1 or 2.
    rhs: an array of rank 1 or 2.
    precision: Optional. Either `None`, which means the default precision for
      the backend, or a `Precision` enum value.

  Returns:
    An array containing the product.
  """
  # TODO(b/134526360): XLA doesn't support integer dots, so we emit a sum of
  # products instead.
  if onp.issubdtype(lhs.dtype, onp.integer):
    lhs_shape = onp.shape(lhs)
    lhs_ndim = len(lhs_shape)
    rhs_ndim = onp.ndim(rhs)
    if rhs_ndim > 1:
      lhs = broadcast_in_dim(lhs, lhs_shape + (1,), tuple(range(len(lhs_shape))))
    if lhs_ndim > 1:
      rhs = broadcast(rhs, (1,))
    return reduce(mul(lhs, rhs), _zero(lhs), add, (len(lhs_shape) - 1,))

  return dot_p.bind(lhs, rhs, precision=_canonicalize_precision(precision))

def dot_general(lhs, rhs, dimension_numbers, precision=None):
  """More general contraction operator.

  Wraps XLA's `DotGeneral
  <https://www.tensorflow.org/xla/operation_semantics#dotgeneral>`_
  operator.

  Args:
    lhs: an array
    rhs: an array
    dimension_numbers: a tuple of tuples of the form
      `((lhs_contracting_dims, rhs_contracting_dims),
      (lhs_batch_dims, rhs_batch_dims))`
    precision: Optional. Either `None`, which means the default precision for
      the backend, or a `Precision` enum value.

  Returns:
    An array containing the result.
  """
  contract_dims, batch_dims = dimension_numbers
  contract_dims = tuple(map(tuple, contract_dims))
  batch_dims = tuple(map(tuple, batch_dims))
  if onp.issubdtype(lhs.dtype, onp.integer):
    # TODO(b/134526360): XLA doesn't support integer dots, so we emit a sum of
    # products instead.
    lhs_contract_dims, rhs_contract_dims = contract_dims
    lhs_batch_dims, rhs_batch_dims = batch_dims
    lhs_noncontract_dims = tuple(sorted(
      set(range(onp.ndim(lhs))) - set(lhs_batch_dims) - set(lhs_contract_dims)))
    rhs_noncontract_dims = tuple(sorted(
      set(range(onp.ndim(rhs))) - set(rhs_batch_dims) - set(rhs_contract_dims)))
    lhs = transpose(lhs,
                    lhs_batch_dims + lhs_noncontract_dims + lhs_contract_dims)
    rhs = transpose(rhs,
                    rhs_batch_dims + rhs_noncontract_dims + rhs_contract_dims)
    new_lhs_shape = onp.insert(
      onp.shape(lhs), len(lhs_batch_dims) + len(lhs_noncontract_dims),
      (1,) * len(rhs_noncontract_dims))
    new_rhs_shape = onp.insert(onp.shape(rhs), len(lhs_batch_dims),
                               (1,) * len(lhs_noncontract_dims))
    lhs = reshape(lhs, new_lhs_shape)
    rhs = reshape(rhs, new_rhs_shape)
    out_ndim = (len(lhs_batch_dims) + len(lhs_noncontract_dims) +
                len(rhs_noncontract_dims))
    return reduce(mul(lhs, rhs), _zero(lhs), add,
                  tuple(range(out_ndim, out_ndim + len(lhs_contract_dims))))

  return dot_general_p.bind(lhs, rhs,
                            dimension_numbers=(contract_dims, batch_dims),
                            precision=_canonicalize_precision(precision))

def broadcast(operand, sizes):
  """Broadcasts an array, adding new major dimensions.

  Wraps XLA's `Broadcast
  <https://www.tensorflow.org/xla/operation_semantics#broadcast>`_
  operator.

  Args:
    operand: an array
    sizes: a sequence of integers, giving the sizes of new major dimensions
      to add.

  Returns:
    An array containing the result.
  """
  return broadcast_p.bind(operand, sizes=tuple(sizes))

def broadcast_in_dim(operand, shape, broadcast_dimensions):
  if operand.ndim == len(shape) and not len(broadcast_dimensions):
    return operand
  if any(x < 0 or x >= len(shape) for x in broadcast_dimensions):
    msg = ("broadcast dimensions must be >= 0 and < ndim(shape), got {} for "
           "shape {}")
    raise ValueError(msg.format(broadcast_dimensions, shape))
  return broadcast_in_dim_p.bind(
      operand, shape=tuple(shape),
      broadcast_dimensions=tuple(broadcast_dimensions))

def reshape(operand, new_sizes, dimensions=None):
  """Wraps XLA's `Reshape
  <https://www.tensorflow.org/xla/operation_semantics#reshape>`_
  operator.
  """
  new_sizes = _canonicalize_shape(new_sizes)  # TODO
  new_sizes = tuple(new_sizes)
  same_shape = onp.shape(operand) == new_sizes
  same_dims = dimensions is None or tuple(dimensions) == tuple(range(onp.ndim(operand)))
  if onp.shape(operand) and same_shape and same_dims:
    return operand
  else:
    return reshape_p.bind(
        operand, new_sizes=new_sizes,
        dimensions=None if same_dims else tuple(dimensions),
        old_sizes=onp.shape(operand))

def pad(operand, padding_value, padding_config):
  """Wraps XLA's `Pad
  <https://www.tensorflow.org/xla/operation_semantics#pad>`_
  operator.
  """
  return pad_p.bind(operand, padding_value, padding_config=tuple(padding_config))

def rev(operand, dimensions):
  """Wraps XLA's `Rev
  <https://www.tensorflow.org/xla/operation_semantics#rev_reverse>`_
  operator.
  """
  return rev_p.bind(operand, dimensions=tuple(dimensions))

def select(pred, on_true, on_false):
  """Wraps XLA's `Select
  <https://www.tensorflow.org/xla/operation_semantics#select>`_
  operator.
  """
  return select_p.bind(pred, on_true, on_false)

def slice(operand, start_indices, limit_indices, strides=None):
  """Wraps XLA's `Slice
  <https://www.tensorflow.org/xla/operation_semantics#slice>`_
  operator.
  """
  if (onp.all(onp.equal(start_indices, 0))
      and onp.all(onp.equal(limit_indices, operand.shape))
      and strides is None):
    return operand
  else:
    return slice_p.bind(operand, start_indices=tuple(start_indices),
                        limit_indices=tuple(limit_indices),
                        strides=None if strides is None else tuple(strides),
                        operand_shape=operand.shape)

def dynamic_slice(operand, start_indices, slice_sizes):
  """Wraps XLA's `DynamicSlice
  <https://www.tensorflow.org/xla/operation_semantics#dynamicslice>`_
  operator.

  Args:
    operand: an array to slice.
    start_indices: a list of scalar indices, one per dimension.
    slice_sizes: the size of the slice. Must be a sequence of non-negative
      integers with length equal to `ndim(operand)`.

  Returns:
    An array containing the slice.
  """
  start_indices = _dynamic_slice_indices(operand, start_indices)
  return dynamic_slice_p.bind(
      operand, *start_indices, slice_sizes=tuple(slice_sizes),
      operand_shape=operand.shape)

def dynamic_update_slice(operand, update, start_indices):
  """Wraps XLA's `DynamicUpdateSlice
  <https://www.tensorflow.org/xla/operation_semantics#dynamicupdateslice>`_
  operator.

  Args:
    operand: an array to slice.
    update: an array containing the new values to write onto `operand`.
    start_indices: a list of scalar indices, one per dimension.

  Returns:
    An array containing the slice.
  """
  start_indices = _dynamic_slice_indices(operand, start_indices)
  return dynamic_update_slice_p.bind(operand, update, *start_indices,
                                     update_shape=update.shape)

def gather(operand, start_indices, dimension_numbers, slice_sizes):
  """Gather operator.

  Wraps `XLA's Gather operator
  <https://www.tensorflow.org/xla/operation_semantics#gather>`_.

  The semantics of gather are complicated, and its API might change in the
  future. For most use cases, you should prefer `Numpy-style indexing
  <https://docs.scipy.org/doc/numpy-1.16.0/reference/arrays.indexing.html>`_
  (e.g., `x[:, (1,4,7), ...]`), rather than using `gather` directly.

  Args:
    operand: an array from which slices should be taken
    start_indices: the indices at which slices should be taken
    dimension_numbers: a `lax.GatherDimensionNumbers` object that describes
      how dimensions of `operand`, `start_indices` and the output relate.
    slice_sizes: the size of each slice. Must be a sequence of non-negative
      integers with length equal to `ndim(operand)`.

  Returns:
    An array containing the gather output.
  """
  return gather_p.bind(
      operand, start_indices, dimension_numbers=dimension_numbers,
      slice_sizes=_canonicalize_shape(slice_sizes), operand_shape=operand.shape)

def scatter_add(operand, scatter_indices, updates, dimension_numbers):
  """Scatter-add operator.

  Wraps `XLA's Scatter operator
  <https://www.tensorflow.org/xla/operation_semantics#scatter>`_, where
  addition is used to combine updates and values from `operand`.

  The semantics of scatter are complicated and its API is subject to change.

  Args:
    operand: an array to which the scatter should be applied
    scatter_indices: an array that gives the indices in `operand` to which each
      update in `updates` should be applied.
    updates: the updates that should be scattered onto `operand`.
    dimension_numbers: a `lax.ScatterDimensionNumbers` object that describes
      how dimensions of `operand`, `start_indices`, `updates` and the output
      relate.

  Returns:
    An array containing the sum of `operand` and the scattered updates.
  """
  jaxpr, consts = _reduction_jaxpr(add, _abstractify(_const(operand, 0)))
  return scatter_add_p.bind(
      operand, scatter_indices, updates, update_jaxpr=jaxpr,
      update_consts=consts, dimension_numbers=dimension_numbers,
      updates_shape=updates.shape)

def scatter_min(operand, scatter_indices, updates, dimension_numbers):
  """Scatter-min operator.

  Wraps `XLA's Scatter operator
  <https://www.tensorflow.org/xla/operation_semantics#scatter>`_, where
  the `min` function is used to combine updates and values from `operand`.

  The semantics of scatter are complicated and its API is subject to change.

  Args:
    operand: an array to which the scatter should be applied
    scatter_indices: an array that gives the indices in `operand` to which each
      update in `updates` should be applied.
    updates: the updates that should be scattered onto `operand`.
    dimension_numbers: a `lax.ScatterDimensionNumbers` object that describes
      how dimensions of `operand`, `start_indices`, `updates` and the output
      relate.

  Returns:
    An array containing the sum of `operand` and the scattered updates.
  """
  jaxpr, consts = _reduction_jaxpr(min, _abstractify(_const(operand, 0)))
  return scatter_min_p.bind(
      operand, scatter_indices, updates, update_jaxpr=jaxpr,
      update_consts=consts, dimension_numbers=dimension_numbers,
      updates_shape=updates.shape)

def scatter_max(operand, scatter_indices, updates, dimension_numbers):
  """Scatter-max operator.

  Wraps `XLA's Scatter operator
  <https://www.tensorflow.org/xla/operation_semantics#scatter>`_, where
  the `max` function is used to combine updates and values from `operand`.

  The semantics of scatter are complicated and its API is subject to change.

  Args:
    operand: an array to which the scatter should be applied
    scatter_indices: an array that gives the indices in `operand` to which each
      update in `updates` should be applied.
    updates: the updates that should be scattered onto `operand`.
    dimension_numbers: a `lax.ScatterDimensionNumbers` object that describes
      how dimensions of `operand`, `start_indices`, `updates` and the output
      relate.

  Returns:
    An array containing the sum of `operand` and the scattered updates.
  """
  jaxpr, consts = _reduction_jaxpr(max, _abstractify(_const(operand, 0)))
  return scatter_max_p.bind(
      operand, scatter_indices, updates, update_jaxpr=jaxpr,
      update_consts=consts, dimension_numbers=dimension_numbers,
      updates_shape=updates.shape)

def scatter(operand, scatter_indices, updates, dimension_numbers):
  """Scatter-update operator.

  Wraps `XLA's Scatter operator
  <https://www.tensorflow.org/xla/operation_semantics#scatter>`_, where updates
  replace values from `operand`.

  If multiple updates are performed to the same index of operand, they may be
  applied in any order.

  The semantics of scatter are complicated and its API is subject to change.

  Args:
    operand: an array to which the scatter should be applied
    scatter_indices: an array that gives the indices in `operand` to which each
      update in `updates` should be applied.
    updates: the updates that should be scattered onto `operand`.
    dimension_numbers: a `lax.ScatterDimensionNumbers` object that describes
      how dimensions of `operand`, `start_indices`, `updates` and the output
      relate.

  Returns:
    An array containing the sum of `operand` and the scattered updates.
  """
  jaxpr, consts = _reduction_jaxpr(lambda x, y: y, _abstractify(_const(operand, 0)))
  return scatter_p.bind(
      operand, scatter_indices, updates, update_jaxpr=jaxpr,
      update_consts=consts, dimension_numbers=dimension_numbers,
      updates_shape=updates.shape)

def index_take(src, idxs, axes):
  indices = concatenate([reshape(i, [i.shape[0], 1]) for i in idxs], 1)
  indices = indices % onp.array([src.shape[ax] for ax in axes])
  slice_sizes = list(src.shape)
  for ax in axes:
    slice_sizes[ax] = 1
  slice_sizes = tuple(slice_sizes)
  offset_dims = tuple(range(1, src.ndim - indices.shape[1] + 1))
  dnums = GatherDimensionNumbers(
      offset_dims=offset_dims,
      collapsed_slice_dims=axes,
      start_index_map=axes)
  return gather(src, indices, dimension_numbers=dnums, slice_sizes=slice_sizes)

def transpose(operand, permutation):
  """Wraps XLA's `Transpose
  <https://www.tensorflow.org/xla/operation_semantics#transpose>`_
  operator.
  """
  permutation = tuple(permutation)
  if permutation == tuple(range(len(permutation))):
    return operand
  else:
    return transpose_p.bind(operand, permutation=permutation)

def reduce(operand, init_value, computation, dimensions):
  """Wraps XLA's `Reduce
  <https://www.tensorflow.org/xla/operation_semantics#reduce>`_
  operator.
  """
  monoid_reducer = _get_monoid_reducer(computation, init_value)
  if monoid_reducer:
    return monoid_reducer(operand, dimensions)
  else:
    jaxpr, consts = _reduction_jaxpr(computation, _abstractify(init_value))
    return reduce_p.bind(operand, init_value, computation=computation,
                         jaxpr=jaxpr, consts=consts, dimensions=tuple(dimensions))

@cache()
def _reduction_jaxpr(computation, aval):
  pval = pe.PartialVal((aval, core.unit))
  comp = lu.wrap_init(lambda x, y: (computation(x, y),))
  jaxpr, _, consts = pe.trace_to_jaxpr(comp, (pval, pval), instantiate=False)
  return jaxpr, consts

def _get_monoid_reducer(monoid_op, x):
  aval = core.get_aval(x)
  dtype = _dtype(x)
  if (type(aval) is ConcreteArray) and aval.shape == ():
    if monoid_op is add:
      return aval.val == 0 and _reduce_sum
    if monoid_op is mul:
      return aval.val == 1 and _reduce_prod
    elif monoid_op is bitwise_or and dtype == onp.bool_:
      return aval.val == _get_max_identity(dtype) and _reduce_or
    elif monoid_op is bitwise_and and dtype == onp.bool_:
      return aval.val == _get_min_identity(dtype) and _reduce_and
    elif monoid_op is max:
      return aval.val == _get_max_identity(dtype) and _reduce_max
    elif monoid_op is min:
      return aval.val == _get_min_identity(dtype) and _reduce_min

def _get_max_identity(dtype):
  if onp.issubdtype(dtype, onp.inexact):
    return onp.array(-onp.inf, dtype)
  elif onp.issubdtype(dtype, onp.integer):
    return onp.array(onp.iinfo(dtype).min, dtype)
  elif onp.issubdtype(dtype, onp.bool_):
    return onp.array(False, onp.bool_)

def _get_min_identity(dtype):
  if onp.issubdtype(dtype, onp.inexact):
    return onp.array(onp.inf, dtype)
  elif onp.issubdtype(dtype, onp.integer):
    return onp.array(onp.iinfo(dtype).max, dtype)
  elif onp.issubdtype(dtype, onp.bool_):
    return onp.array(True, onp.bool_)

def _reduce_sum(operand, axes):
  return reduce_sum_p.bind(operand, axes=tuple(axes),
                           input_shape=onp.shape(operand))

def _reduce_prod(operand, axes):
  return reduce_prod_p.bind(operand, axes=tuple(axes))

def _reduce_max(operand, axes):
  return reduce_max_p.bind(operand, axes=tuple(axes))

def _reduce_min(operand, axes):
  return reduce_min_p.bind(operand, axes=tuple(axes))

def _reduce_or(operand, axes):
  return reduce_or_p.bind(operand, axes=tuple(axes))

def _reduce_and(operand, axes):
  return reduce_and_p.bind(operand, axes=tuple(axes))

def reduce_window(operand, init_value, computation, window_dimensions,
                  window_strides, padding):
  """Wraps XLA's `ReduceWindow
  <https://www.tensorflow.org/xla/operation_semantics#reducewindow>`_
  operator.
  """
  monoid_reducer = _get_monoid_window_reducer(computation, init_value)
  if monoid_reducer:
    return monoid_reducer(operand, window_dimensions, window_strides, padding)
  else:
    jaxpr, consts = _reduction_jaxpr(computation, _abstractify(init_value))
    return reduce_window_p.bind(
        operand, init_value, jaxpr=jaxpr, consts=consts,
        window_dimensions=tuple(window_dimensions),
        window_strides=tuple(window_strides), padding=padding)

def _get_monoid_window_reducer(monoid_op, x):
  aval = core.get_aval(x)
  if (type(aval) is ConcreteArray) and aval.shape == ():
    if monoid_op is add:
      return aval.val == 0 and _reduce_window_sum
    elif monoid_op is max:
      return aval.val == _get_max_identity(aval.dtype) and _reduce_window_max
    elif monoid_op is min:
      return aval.val == _get_min_identity(aval.dtype) and _reduce_window_min

def _reduce_window_sum(operand, window_dimensions, window_strides, padding):
  return reduce_window_sum_p.bind(
      operand, window_dimensions=tuple(window_dimensions),
      window_strides=tuple(window_strides), padding=padding,
      input_shape=operand.shape)

def _reduce_window_prod(operand, window_dimensions, window_strides, padding):
  init_value = _const(operand, 1)
  jaxpr, consts = _reduction_jaxpr(mul, _abstractify(init_value))
  return reduce_window_p.bind(
      operand, init_value, jaxpr=jaxpr, consts=consts,
      window_dimensions=tuple(window_dimensions),
      window_strides=tuple(window_strides), padding=padding)

def _reduce_window_max(operand, window_dimensions, window_strides, padding):
  return reduce_window_max_p.bind(
      operand, window_dimensions=tuple(window_dimensions),
      window_strides=tuple(window_strides), padding=padding)

def _reduce_window_min(operand, window_dimensions, window_strides, padding):
  return reduce_window_min_p.bind(
      operand, window_dimensions=tuple(window_dimensions),
      window_strides=tuple(window_strides), padding=padding)

def _select_and_scatter(operand, select, window_dimensions, window_strides,
                        padding, source, init_value, scatter):
  select_jaxpr, select_consts = _reduction_jaxpr(select, _abstractify(init_value))
  scatter_jaxpr, scatter_consts = _reduction_jaxpr(scatter, _abstractify(init_value))
  return select_and_scatter_p.bind(
      operand, source, init_value, select_jaxpr=select_jaxpr,
      select_consts=select_consts, scatter_jaxpr=scatter_jaxpr,
      scatter_consts=scatter_consts, window_dimensions=tuple(window_dimensions),
      window_strides=tuple(window_strides), padding=padding)

def _select_and_scatter_add(source, operand, select_prim, window_dimensions,
                            window_strides, padding):
  return select_and_scatter_add_p.bind(
      source, operand, select_prim=select_prim,
      window_dimensions=tuple(window_dimensions),
      window_strides=tuple(window_strides), padding=padding)

def _select_and_gather_add(tangents, operand, select_prim, window_dimensions,
                           window_strides, padding):
  return select_and_gather_add_p.bind(
      tangents, operand, select_prim=select_prim,
      window_dimensions=tuple(window_dimensions),
      window_strides=tuple(window_strides), padding=padding)

def sort(operand, dimension=-1):
  """Wraps XLA's `Sort
  <https://www.tensorflow.org/xla/operation_semantics#sort>`_
  operator.
  """
  return sort_p.bind(operand, dimension=dimension)

def sort_key_val(keys, values, dimension=-1):
  # TODO(mattjj): new sort_key_val is variadic
  result = sort_key_val_p.bind(keys, values, dimension=dimension)
  sorted_keys, sorted_values = result
  return sorted_keys, sorted_values


def tie_in(x, y):
  return tie_in_p.bind(x, y)

def shaped_identity(x):
  return shaped_identity_p.bind(x, shape=x.shape)


def full(shape, fill_value, dtype=None):
  """Returns an array of `shape` filled with `fill_value`.

  Arguments:
    shape: sequence of integers, describing the shape of the output array
    fill_value: the value to fill the new array with
    dtype: the type of the output array, or `None`. If not `None`, `fill_value`
      will be cast to `dtype`.
  """
  try:
    shape = _canonicalize_shape(shape)
  except TypeError:
    msg = ("`full` requires shapes to be concrete. If using `jit`, try using "
           "`static_argnums` or applying `jit` to smaller subfunctions instead.")
    raise TypeError(msg)

  if onp.shape(fill_value):
    msg = "full must be called with scalar fill_value, got fill_value.shape {}."
    raise TypeError(msg.format(onp.shape(fill_value)))
  dtype = dtype or _dtype(fill_value)
  dtype = xla_bridge.canonicalize_dtype(dtype)

  # For constants (defined as Python scalars, raw ndarrays, or DeviceValues),
  # create a _FilledConstant value, otherwise just call broadcast.
  if onp.isscalar(fill_value) or type(fill_value) is onp.ndarray:
    return _FilledConstant(onp.asarray(fill_value, dtype), shape)
  elif isinstance(fill_value, xla.DeviceValue):
    val = onp.asarray(fill_value, dtype)
    return _FilledConstant(val, shape)
  else:
    return broadcast(convert_element_type(fill_value, dtype), shape)

def iota(dtype, size):
  """Wraps XLA's `Iota
  <https://www.tensorflow.org/xla/operation_semantics#iota>`_
  operator.
  """
  return broadcasted_iota(dtype, (int(size),), 0)

def broadcasted_iota(dtype, shape, dimension):
  """Wraps XLA's `Iota
  <https://www.tensorflow.org/xla/operation_semantics#iota>`_
  operator.
  """
  dtype = xla_bridge.canonicalize_dtype(dtype)
  shape = _canonicalize_shape(shape)
  dimension = int(dimension)
  return _IotaConstant(dtype, shape, dimension)

def eye(dtype, size):
  return broadcasted_eye(dtype, (size, size), (0, 1))

def broadcasted_eye(dtype, shape, axes):
  if not isinstance(axes, (list, tuple)) or not len(axes) >= 2:
    raise TypeError("make_diagonal `axes` must be a tuple with len at least 2.")
  dtype = xla_bridge.canonicalize_dtype(dtype)
  shape = _canonicalize_shape(shape)
  axes = tuple(map(int, axes))
  return _EyeConstant(shape, axes, dtype)


def stop_gradient(x):
  """Stops gradient computation.

   Operationally `stop_gradient` is the identity function, that is, it returns
   argument `x` unchanged. However, `stop_gradient` prevents the flow of
   gradients during forward or reverse-mode automatic differentiation. If there
   are multiple nested gradient computations, `stop_gradient` stops gradients
   for all of them.

   For example:

   >>> jax.grad(lambda x: x**2)(3.)
   array(6., dtype=float32)
   >>> jax.grad(lambda x: jax.lax.stop_gradient(x)**2)(3.)
   array(0., dtype=float32)
   >>> jax.grad(jax.grad(lambda x: x**2))(3.)
   array(2., dtype=float32)
   >>> jax.grad(jax.grad(lambda x: jax.lax.stop_gradient(x)**2))(3.)
   array(0., dtype=float32)
   """
  return tree_map(stop_gradient_p.bind, x)


def _safe_mul(x, y): return safe_mul_p.bind(x, y)


### convenience wrappers around traceables


def conv(lhs, rhs, window_strides, padding, precision=None):
  """Convenience wrapper around `conv_general_dilated`.

  Args:
    lhs: a rank `n+2` dimensional input array.
    rhs: a rank `n+2` dimensional array of kernel weights.
    window_strides: a sequence of `n` integers, representing the inter-window
      strides.
    padding: either the string `'SAME'`, the string `'VALID'`.
    precision: Optional. Either `None`, which means the default precision for
      the backend, or a `Precision` enum value.

  Returns:
    An array containing the convolution result.
  """
  pads = padtype_to_pads(lhs.shape[2:], rhs.shape[2:], window_strides, padding)
  return conv_general_dilated(lhs, rhs, window_strides, padding,
                              precision=precision)

def conv_with_general_padding(lhs, rhs, window_strides, padding,
                              lhs_dilation, rhs_dilation, precision=None):
  """Convenience wrapper around `conv_general_dilated`.

  Args:
    lhs: a rank `n+2` dimensional input array.
    rhs: a rank `n+2` dimensional array of kernel weights.
    window_strides: a sequence of `n` integers, representing the inter-window
      strides.
    padding: either the string `'SAME'`, the string `'VALID'`, or a sequence of
      `n` `(low, high)` integer pairs that give the padding to apply before and
      after each spatial dimension.
    lhs_dilation: `None`, or a sequence of `n` integers, giving the
      dilation factor to apply in each spatial dimension of `lhs`. LHS dilation
      is also known as transposed convolution.
    rhs_dilation: `None`, or a sequence of `n` integers, giving the
      dilation factor to apply in each spatial dimension of `rhs`. RHS dilation
      is also known as atrous convolution.
    precision: Optional. Either `None`, which means the default precision for
      the backend, or a `Precision` enum value.

  Returns:
    An array containing the convolution result.
  """
  return conv_general_dilated(
      lhs, rhs, window_strides, padding, lhs_dilation=lhs_dilation,
      rhs_dilation=rhs_dilation, precision=precision)


def _conv_transpose_padding(k, s, padding):
  """Calculate before and after padding for a dim of transposed convolution.

  Args:
    k: int: kernel dimension.
    s: int: dimension stride value.
    padding: 'same' or 'valid' padding mode for original forward conv.

  Returns:
    2-tuple: ints: before and after padding for transposed convolution.
  """
  if padding == 'SAME':
    pad_len = k + s - 2
    if s > k - 1:
      pad_a = k - 1
    else:
      pad_a = int(onp.ceil(pad_len / 2))
  elif padding == 'VALID':
    pad_len = k + s - 2 + _max(k - s, 0)
    pad_a = k - 1
  else:
    raise ValueError('Padding mode must be `SAME` or `VALID`.')
  pad_b = pad_len - pad_a
  return pad_a, pad_b


def _flip_axes(x, axes):
  """Flip ndarray 'x' along each axis specified in axes tuple."""
  for axis in axes:
    x = onp.flip(x, axis)
  return x


def conv_transpose(lhs, rhs, strides, padding, dimension_numbers=None,
                   transpose_kernel=False, precision=None):
  """Convenience wrapper for calculating the N-d convolution "transpose".

  This function directly calculates a fractionally strided conv rather than
  indirectly calculating the gradient (transpose) of a forward convolution.

  Args:
    lhs: a rank `n+2` dimensional input array.
    rhs: a rank `n+2` dimensional array of kernel weights.
    strides: sequence of `n` integers, sets fractional stride.
    padding: 'SAME', 'VALID' will set as transpose of corresponding forward
      conv, or a sequence of `n` integer 2-tuples describing before-and-after
      padding for each `n` spatial dimension.
    dimension_numbers: tuple of dimension descriptors as in
      lax.conv_general_dilated. Defaults to tensorflow convention.
    transpose_kernel: if True flips spatial axes and swaps the input/output
      channel axes of the kernel. This makes the output of this function identical
      to the gradient-derived functions like keras.layers.Conv2DTranspose
      applied to the same kernel. For typical use in neural nets this is completely
      pointless and just makes input/output channel specification confusing.
    precision: Optional. Either `None`, which means the default precision for
      the backend, or a `Precision` enum value.

  Returns:
    Transposed N-d convolution, with output padding following the conventions of
    keras.layers.Conv2DTranspose.
  """
  assert len(lhs.shape) == len(rhs.shape) and len(lhs.shape) > 2
  ndims = len(lhs.shape)
  one = (1,) * (ndims - 2)
  # Set dimensional layout defaults if not specified.
  if dimension_numbers is None:
    if ndims == 3:
      dimension_numbers = ('NHC', 'HIO', 'NHC')
    elif ndims == 4:
      dimension_numbers = ('NHWC', 'HWIO', 'NHWC')
    elif ndims == 5:
      dimension_numbers = ('NHWDC', 'HWDIO', 'NHWDC')
    else:
      raise ValueError('No 4+ dimensional dimension_number defaults.')
  dn = conv_dimension_numbers(lhs.shape, rhs.shape, dimension_numbers)
  k_shape = onp.take(rhs.shape, dn.rhs_spec)
  k_sdims = k_shape[2:]
  # Calculate correct output shape given padding and strides.
  if padding in {'SAME', 'VALID'}:
    pads = [_conv_transpose_padding(k, s, padding)
            for k,s in zip(k_sdims.tolist(), strides)]
  else:
    pads = padding
  if transpose_kernel:
    # flip spatial dims and swap input / output channel axes
    rhs = _flip_axes(rhs, onp.array(dn.rhs_spec)[2:])
    rhs = onp.swapaxes(rhs, dn.rhs_spec[0], dn.rhs_spec[1])
  return conv_general_dilated(lhs, rhs, one, pads, strides, one, dn,
                              precision=precision)


def full_like(x, fill_value, dtype=None, shape=None):
  """Create a full array like np.full based on the example array `x`.

  Args:
    x: example array-like, used for shape and dtype information.
    fill_value: a scalar value to fill the entries of the output array.
    dtype: optional, a dtype parameter for the output ndarray.
    shape: optional, a shape parameter for the output ndarray.

  Returns:
    An ndarray with the same shape as `x` with its entries set equal to
    `fill_value`, similar to the output of np.full.
  """
  shape = onp.shape(x) if shape is None else _canonicalize_shape(shape)
  out = full(shape, fill_value, dtype or _dtype(x))
  return tie_in(x, out)


def collapse(operand, start_dimension, stop_dimension):
  lo, hi = start_dimension, stop_dimension
  size = prod(operand.shape[lo:hi])
  new_shape = operand.shape[:lo] + (size,) + operand.shape[hi:]
  return reshape(operand, new_shape)


def slice_in_dim(operand, start_index, limit_index, stride=1, axis=0):
  """Convenience wrapper around slice applying to only one dimension."""
  start_indices = [0] * operand.ndim
  limit_indices = list(operand.shape)
  strides = [1] * operand.ndim

  axis = int(axis)
  start_indices[axis] = int(start_index)
  limit_indices[axis] = int(limit_index)
  strides[axis] = int(stride)

  return slice(operand, start_indices, limit_indices, strides)


def index_in_dim(operand, index, axis=0, keepdims=True):
  """Convenience wrapper around slice to perform int indexing."""
  index, axis = int(index), int(axis)
  axis_size = operand.shape[axis]
  wrapped_index = index + axis_size if index < 0 else index
  if not 0 <= wrapped_index < axis_size:
    msg = 'index {} is out of bounds for axis {} with size {}'
    raise IndexError(msg.format(index, axis, axis_size))
  result = slice_in_dim(operand, wrapped_index, wrapped_index + 1, 1, axis)
  if keepdims:
    return result
  else:
    return reshape(result, onp.delete(operand.shape, axis))


def dynamic_slice_in_dim(operand, start_index, slice_size, axis=0):
  """Convenience wrapper around dynamic_slice applying to one dimension."""
  start_indices = [0] * operand.ndim
  slice_sizes = list(operand.shape)

  axis = int(axis)
  start_indices[axis] = start_index
  slice_sizes[axis] = int(slice_size)
  return dynamic_slice(operand, start_indices, slice_sizes)


def dynamic_index_in_dim(operand, index, axis=0, keepdims=True):
  """Convenience wrapper around dynamic_slice to perform int indexing."""
  result = dynamic_slice_in_dim(operand, index, 1, axis)
  if keepdims:
    return result
  else:
    return reshape(result, onp.delete(operand.shape, axis))


def dynamic_update_slice_in_dim(operand, update, start_index, axis):
  axis = int(axis)
  start_indices = [0] * _ndim(operand)
  start_indices[axis] = start_index
  return dynamic_update_slice(operand, update, start_indices)


def dynamic_update_index_in_dim(operand, update, index, axis):
  axis = int(axis)
  if _ndim(update) != _ndim(operand):
    assert _ndim(update) + 1 == _ndim(operand)
    ax = axis % _ndim(operand)
    update = reshape(update, operand.shape[:ax] + (1,) + operand.shape[ax+1:])
  return dynamic_update_slice_in_dim(operand, update, index, axis)


def batch_matmul(lhs, rhs):
  """Batch matrix multiplication."""
  if _min(lhs.ndim, rhs.ndim) < 2:
    raise ValueError('Arguments to batch_matmul must be at least 2D, got {}, {}'
                     .format(lhs.ndim, rhs.ndim))
  if lhs.ndim != rhs.ndim:
    raise ValueError('Arguments to batch_matmul must have same ndim, got {}, {}'
                     .format(lhs.ndim, rhs.ndim))
  lhs_contract = (lhs.ndim - 1,)
  rhs_contract = (rhs.ndim - 2,)
  batch = tuple(range(lhs.ndim - 2))
  return dot_general(lhs, rhs, [(lhs_contract, rhs_contract), (batch, batch)])


# These functions also exist in the XLA client library, but we treat them
# as non-primitive to maintain a smaller set of autodiff primitives.

def square(x):
  r"""Elementwise square: :math:`x^2`."""
  return mul(x, x)

def reciprocal(x):
  r"""Elementwise reciprocal: :math:`1 \over x`."""
  return div(_const(x, 1), x)

def tan(x):
  r"""Elementwise tangent: :math:`\mathrm{tan}(x)`."""
  return div(sin(x), cos(x))

def asin(x):
  r"""Elementwise arc sine: :math:`\mathrm{asin}(x)`."""
  return mul(_const(x, 2),
             atan2(x, add(_const(x, 1), sqrt(sub(_const(x, 1), square(x))))))

def acos(x):
  r"""Elementwise arc cosine: :math:`\mathrm{acos}(x)`."""
  return select(
      ne(x, _const(x, -1.0)),
      mul(_const(x, 2),
          atan2(sqrt(sub(_const(x, 1), square(x))), add(_const(x, 1), x))),
      full_like(x, onp.pi))

def atan(x):
  r"""Elementwise arc tangent: :math:`\mathrm{atan}(x)`."""
  return atan2(x, _const(x, 1))

def sinh(x):
  r"""Elementwise hyperbolic sine: :math:`\mathrm{sinh}(x)`."""
  log_half = _const(x, onp.log(0.5))
  # This formulation avoids overflow when e^x is inf but e^x/2 is not inf.
  return sub(exp(add(log_half, x)), exp(sub(log_half, x)))

def cosh(x):
  r"""Elementwise hyperbolic cosine: :math:`\mathrm{cosh}(x)`."""
  log_half = _const(x, onp.log(0.5))
  # This formulation avoids overflow when e^x is inf but e^x/2 is not inf.
  return add(exp(add(log_half, x)), exp(sub(log_half, x)))


# Add some methods to ShapedArray that rely on lax primitives

ShapedArray.broadcast = core.aval_method(broadcast)
ShapedArray.transpose = core.aval_method(transpose)  # clobbered by lax_numpy
ShapedArray.reshape = core.aval_method(reshape)      # clobbered by lax_numpy

def _iter(tracer):
  if tracer.ndim == 0:
    raise TypeError("iteration over a 0-d array")  # same as numpy error
  else:
    n = tracer.shape[0]
    # return (index_in_dim(tracer, i, keepdims=False) for i in xrange(n))
    return iter([index_in_dim(tracer, i, keepdims=False) for i in xrange(n)])
ShapedArray._iter = staticmethod(_iter)

# Add some ad handlers that use (or could use) lax primitives

def zeros_like_array(x):
  return full_like(x, 0)

for t in itertools.chain(array_types, [xla.DeviceArray]):
  ad_util.jaxval_adders[t] = add
ad_util.jaxval_zeros_likers[xla.DeviceArray] = zeros_like_array


### primitives


_input_dtype = lambda *args, **_: xla_bridge.canonicalize_dtype(args[0].dtype)
_fixed_dtype = lambda dtype: lambda *args, **kwargs: xla_bridge.canonicalize_dtype(dtype)
_complex_basetype = lambda dtype: onp.abs(onp.zeros((), dtype)).dtype

def standard_primitive(shape_rule, dtype_rule, name, translation_rule=None):
  prim = Primitive(name)
  prim.def_impl(partial(xla.apply_primitive, prim))
  prim.def_abstract_eval(partial(standard_abstract_eval, shape_rule, dtype_rule))
  xla.translations[prim] = translation_rule or partial(standard_translate, name)
  return prim


def standard_reduction_primitive(shape_rule, dtype_rule, name, translation_rule=None):
  prim = Primitive(name)
  prim.def_impl(partial(xla.apply_primitive, prim))
  prim.def_abstract_eval(partial(standard_abstract_eval, shape_rule, dtype_rule))
  xla.reduction_translations[prim] = translation_rule or partial(standard_translate, name)
  return prim


def standard_abstract_eval(shape_rule, dtype_rule, *args, **kwargs):
  assert all(isinstance(arg, UnshapedArray) for arg in args), args
  least_specialized = _max(
      map(type, args), key=operator.attrgetter('array_abstraction_level'))
  if least_specialized is ConcreteArray:
    return ShapedArray(shape_rule(*args, **kwargs), dtype_rule(*args, **kwargs))
  elif least_specialized is ShapedArray:
    return ShapedArray(shape_rule(*args, **kwargs), dtype_rule(*args, **kwargs))
  elif least_specialized is UnshapedArray:
    return UnshapedArray(dtype_rule(*args, **kwargs))
  else:
    raise TypeError(args, least_specialized)


def standard_translate(name, c, *args, **kwargs):
  xla_opname = ''.join(term.capitalize() for term in name.split('_'))
  return getattr(c, xla_opname)(*args, **kwargs)


def unop_dtype_rule(result_dtype, accepted_dtypes, name, aval, **kwargs):
  if not any(onp.issubdtype(aval.dtype, t) for t in accepted_dtypes):
    msg = '{} does not accept dtype {}. Accepted dtypes are subtypes of {}.'
    typename = str(onp.dtype(aval.dtype).name)
    accepted_typenames = (str(onp.dtype(t).name) for t in accepted_dtypes)
    raise TypeError(msg.format(name, typename, ', '.join(accepted_typenames)))
  return result_dtype(aval.dtype)


def unop(result_dtype, accepted_dtypes, name):
  dtype_rule = partial(unop_dtype_rule, result_dtype, accepted_dtypes, name)
  prim = standard_primitive(_attrgetter('shape'), dtype_rule, name)
  batching.defvectorized(prim)
  masking.defvectorized(prim)
  return prim
standard_unop = partial(unop, _identity)
_attrgetter = lambda name: lambda x, **kwargs: getattr(x, name)


def binop_dtype_rule(result_dtype, accepted_dtypes, name, *avals, **kwargs):
  aval_dtypes = [aval.dtype for aval in avals]
  for i, (aval_dtype, types) in enumerate(zip(aval_dtypes, accepted_dtypes)):
    if not any(onp.issubdtype(aval_dtype, t) for t in types):
      msg = ('{} does not accept dtype {} at position {}. '
             'Accepted dtypes at position {} are subtypes of {}.')
      typename = str(onp.dtype(aval_dtype).name)
      typenames = ', '.join(str(onp.dtype(t).name) for t in types)
      raise TypeError(msg.format(name, typename, i, i, typenames))
  _check_same_dtypes(name, False, *aval_dtypes)
  return result_dtype(*avals)


def _broadcasting_shape_rule(name, *avals):
  shapes = onp.array([aval.shape for aval in avals if aval.shape])
  if not shapes.size:
    return ()
  if len({len(shape) for shape in shapes}) != 1:
    msg = '{} got arrays of different rank: {}.'
    raise TypeError(msg.format(name, ', '.join(map(str, map(tuple, shapes)))))
  min_shape = onp.min(shapes, axis=0)
  max_shape = onp.max(shapes, axis=0)
  result_shape = onp.where(min_shape == 0, 0, max_shape)
  if not onp.all((shapes == result_shape) | (shapes == 1)):
    msg = '{} got incompatible shapes for broadcasting: {}.'
    raise TypeError(msg.format(name, ', '.join(map(str, map(tuple, shapes)))))
  return tuple(result_shape)


def binop(result_dtype, accepted_dtypes, name, translation_rule=None):
  dtype_rule = partial(binop_dtype_rule, result_dtype, accepted_dtypes, name)
  shape_rule = partial(_broadcasting_shape_rule, name)
  prim = standard_primitive(shape_rule, dtype_rule, name,
                            translation_rule=translation_rule)
  batching.defbroadcasting(prim)
  masking.defbinop(prim)
  return prim
standard_binop = partial(binop, _input_dtype)


# NOTE(mattjj): this isn't great for orchestrate fwd mode because it means JVPs
# get two extra ops in them: a reshape and a broadcast_in_dim (or sometimes just
# a broadcast). but saving the shape info with the primitives isn't great either
# because then we can't trace these ops without shape data.
def _brcast(x, *others):
  # Used in jvprules to make binop broadcasting explicit for transposability.
  # Requires shape info during jvp tracing, which isn't strictly necessary.
  # We don't need full numpy broadcasting, but otherwise the logic is the same
  # so we reuse the broadcast_shapes function after filtering out scalars.
  shapes = tuple(filter(None, map(onp.shape, (x,) + others)))
  shape = shapes and broadcast_shapes(*shapes)
  if onp.shape(x) != shape:
    return _brcast_to(x, shape)
  else:
    return x


def _brcast_to(x, shape):
  x_shape = onp.shape(x)
  assert x_shape != shape
  if x_shape:
    assert len(x_shape) == len(shape)
    broadcast_dimensions, = onp.where(onp.equal(x_shape, shape))
    squeezed_dimensions, = onp.where(onp.not_equal(x_shape, shape))
    inshape = onp.delete(x_shape, squeezed_dimensions)
    return broadcast_in_dim(reshape(x, inshape), shape, broadcast_dimensions)
  else:
    return broadcast(x, shape)


_float = {onp.floating}
_complex = {onp.complexfloating}
_complex_elem_types = {onp.float32, onp.float64}
_int = {onp.integer}
_bool = {onp.bool_}

_num = _int | _float | _complex
_any = _int | _float | _complex | _bool

neg_p = standard_unop(_num, 'neg')
ad.deflinear(neg_p, lambda t: [neg(t)])

sign_p = standard_unop(_num, 'sign')
ad.defjvp_zero(sign_p)

floor_p = standard_unop(_float, 'floor')
ad.defjvp_zero(floor_p)

ceil_p = standard_unop(_float, 'ceil')
ad.defjvp_zero(ceil_p)

round_p = standard_unop(_float, 'round')
ad.defjvp_zero(round_p)

is_finite_p = unop(_fixed_dtype(onp.bool_), _float, 'is_finite')
ad.defjvp_zero(is_finite_p)

exp_p = standard_unop(_float | _complex, 'exp')
ad.defjvp2(exp_p, lambda g, ans, x: _safe_mul(g, ans))

log_p = standard_unop(_float | _complex, 'log')
ad.defjvp(log_p, lambda g, x: div(g, x))

expm1_p = standard_unop(_float | _complex, 'expm1')
ad.defjvp2(expm1_p, lambda g, ans, x: mul(g, add(ans, _one(ans))))

log1p_p = standard_unop(_float | _complex, 'log1p')
ad.defjvp(log1p_p, lambda g, x: div(g, add(x, _one(x))))

tanh_p = standard_unop(_float | _complex, 'tanh')
ad.defjvp2(tanh_p, lambda g, ans, x: mul(g, sub(_one(x), mul(ans, ans))))

sin_p = standard_unop(_float | _complex, 'sin')
ad.defjvp(sin_p, lambda g, x: mul(g, cos(x)))

cos_p = standard_unop(_float | _complex, 'cos')
ad.defjvp(cos_p, lambda g, x: neg(mul(g, sin(x))))

atan2_p = standard_binop([_float, _float], 'atan2')
ad.defjvp(atan2_p,
  lambda g, x, y: _brcast(g, y) * (y / (square(x) + square(y))),
  lambda g, x, y: _brcast(g, x) * -x / (square(x) + square(y)))

lgamma_p = standard_unop(_float, 'lgamma')
ad.defjvp(lgamma_p, lambda g, x: mul(g, digamma(x)))

digamma_p = standard_unop(_float, 'digamma')

erf_p = standard_unop(_float, 'erf')
ad.defjvp(erf_p, lambda g, x: mul(_const(x, 2. / onp.sqrt(onp.pi)),
                                  mul(g, exp(neg(square(x))))))

erfc_p = standard_unop(_float, 'erfc')
ad.defjvp(erfc_p, lambda g, x: mul(_const(x, 2. / onp.sqrt(onp.pi)),
                                   mul(neg(g), exp(neg(square(x))))))

erf_inv_p = standard_unop(_float, 'erf_inv')
ad.defjvp2(erf_inv_p, lambda g, ans, x: mul(_const(x, onp.sqrt(onp.pi) / 2.),
                                            mul(g, exp(square(ans)))))

real_p = unop(_complex_basetype, _complex, 'real')
ad.deflinear(real_p, lambda t: [complex(t, onp.zeros((), _dtype(t)))])

imag_p = unop(_complex_basetype, _complex, 'imag')
ad.defjvp(imag_p, lambda g, _: real(mul(_const(g, -1j), g)))

_complex_dtype = lambda dtype, *args: (onp.zeros((), dtype) + onp.zeros((), onp.complex64)).dtype
complex_p = binop(_complex_dtype, [_complex_elem_types, _complex_elem_types],
                  'complex')
ad.deflinear(complex_p, lambda t: [real(t), imag(neg(t))])

conj_p = unop(_complex_dtype, _float | _complex, 'conj')

def _conj_transpose_rule(t, x, input_dtype):
  assert x is ad.undefined_primal
  if onp.issubdtype(input_dtype, onp.complexfloating):
    return [conj(t)]
  else:
    return [real(t)]

xla.translations[conj_p] = lambda c, x, **kwargs: c.Conj(x)
ad.primitive_jvps[conj_p] = partial(ad.linear_jvp, conj_p)
ad.primitive_transposes[conj_p] = _conj_transpose_rule

abs_p = unop(_complex_basetype, _num, 'abs')
ad.defjvp2(abs_p,
           lambda g, ans, x:
           div(_maybe_real(mul(g, _maybe_conj(x))), _replace_zero(ans)))
_maybe_conj = lambda x: conj(x) if _iscomplex(x) else x
_maybe_real = lambda x: real(x) if _iscomplex(x) else x

sqrt_p = standard_unop(_float | _complex, 'sqrt')
ad.defjvp2(sqrt_p, lambda g, ans, x: _safe_mul(g, div(_const(x, 0.5), ans)))

rsqrt_p = standard_unop(_float | _complex, 'rsqrt')
ad.defjvp2(rsqrt_p,
           lambda g, ans, x:
           _safe_mul(g, mul(_const(x, -0.5), pow(x, _const(x, -1.5)))))

pow_p = standard_binop([_float | _complex, _float | _complex], 'pow')

def _pow_jvp_lhs(g, x, y):
  # we call _safe_mul here so that we get the behavior 0*inf = 0, since when a
  # coefficient in `g` is zero we want to keep it at zero, not produce a nan.
  # see https://github.com/google/jax/pull/383
  jac = mul(y, pow(x, select(eq(y, _zeros(y)), _ones(y), sub(y, _ones(y)))))
  return _safe_mul(_brcast(g, y), jac)

def _pow_jvp_rhs(g, x, y):
  return mul(_brcast(g, x), mul(log(_replace_zero(x)), pow(x, y)))

ad.defjvp(pow_p, _pow_jvp_lhs, _pow_jvp_rhs)
_replace_zero = lambda x: select(eq(x, _const(x, 0)), _ones(x), x)

not_p = standard_unop(_int | _bool, 'not')

and_p = standard_binop([_any, _any], 'and')
ad.defjvp_zero(and_p)

or_p = standard_binop([_any, _any], 'or')
ad.defjvp_zero(or_p)

xor_p = standard_binop([_any, _any], 'xor')
ad.defjvp_zero(xor_p)

def _add_transpose(t, x, y):
  # assert x is ad.undefined_primal and y is ad.undefined_primal  # not affine
  return [t, t]

add_p = standard_binop([_num, _num], 'add')
ad.defjvp(add_p, lambda g, x, y: _brcast(g, y), lambda g, x, y: _brcast(g, x))
ad.primitive_transposes[add_p] = _add_transpose


def _sub_transpose(t, x, y):
  assert x is ad.undefined_primal and y is ad.undefined_primal  # not affine
  return [t, neg(t) if t is not ad_util.zero else ad_util.zero]

sub_p = standard_binop([_num, _num], 'sub')
ad.defjvp(sub_p,
          lambda g, x, y: _brcast(g, y),
          lambda g, x, y: _brcast(neg(g), x))
ad.primitive_transposes[sub_p] = _sub_transpose

mul_p = standard_binop([_num, _num], 'mul')
ad.defbilinear_broadcasting(_brcast, mul_p, mul, mul)


def _safe_mul_translation_rule(c, x, y):
  dtype = c.GetShape(x).numpy_dtype()
  zero = c.Constant(onp.array(0, dtype=dtype))
  out_shape = broadcast_shapes(c.GetShape(x).dimensions(),
                               c.GetShape(y).dimensions())
  return c.Select(c.Or(c.Eq(x, zero), c.Eq(y, zero)),
                  c.Broadcast(zero, out_shape),
                  c.Mul(x, y))

safe_mul_p = standard_binop([_num, _num], 'safe_mul',
                            translation_rule=_safe_mul_translation_rule)
ad.defbilinear_broadcasting(_brcast, safe_mul_p, _safe_mul, _safe_mul)


def _div_transpose_rule(cotangent, x, y):
  assert x is ad.undefined_primal and y is not ad.undefined_primal
  res = ad_util.zero if cotangent is ad_util.zero else div(cotangent, y)
  return res, None
div_p = standard_binop([_num, _num], 'div')
ad.defjvp(div_p,
          lambda g, x, y: div(_brcast(g, y), y),
          lambda g, x, y: div(mul(neg(_brcast(g, x)), x), square(y)))
ad.primitive_transposes[div_p] = _div_transpose_rule

rem_p = standard_binop([_num, _num], 'rem')
ad.defjvp(rem_p,
          lambda g, x, y: _brcast(g, y),
          lambda g, x, y: mul(_brcast(neg(g), x), floor(div(x, y))))


def _broadcasting_select(c, which, x, y):
  """Wrapper around XLA `Select` that broadcasts its arguments."""
  which_shape, x_shape, y_shape = (
    c.GetShape(t).dimensions() for t in (which, x, y))
  out_shape = broadcast_shapes(which_shape, x_shape, y_shape)
  bcast_dims = lambda shape: tuple(range(len(out_shape) - len(shape),
                                         len(out_shape)))
  which = c.BroadcastInDim(which, out_shape, bcast_dims(which_shape))
  x = c.BroadcastInDim(x, out_shape, bcast_dims(x_shape))
  y = c.BroadcastInDim(y, out_shape, bcast_dims(y_shape))
  return c.Select(which, x, y)


def _minmax_translation_rule(c, x, y, minmax=None, cmp=None):
  dtype = c.GetShape(x).numpy_dtype()
  if onp.issubdtype(dtype, onp.complexfloating):
    comparator = cmp(c)
    rx = c.Real(x)
    ry = c.Real(y)
    return _broadcasting_select(
        c, c.Select(c.Eq(rx, ry), comparator(c.Imag(x), c.Imag(y)),
                    comparator(rx, ry)),
        x, y)
  return minmax(c)(x, y)

max_p = standard_binop([_any, _any], 'max', translation_rule=partial(
    _minmax_translation_rule, minmax=lambda c: c.Max, cmp=lambda c: c.Gt))
ad.defjvp2(max_p,
           lambda g, ans, x, y: mul(_brcast(g, y), _balanced_eq(x, ans, y)),
           lambda g, ans, x, y: mul(_brcast(g, x), _balanced_eq(y, ans, x)))

min_p = standard_binop([_any, _any], 'min', translation_rule=partial(
    _minmax_translation_rule, minmax=lambda c: c.Min, cmp=lambda c: c.Lt))
ad.defjvp2(min_p,
           lambda g, ans, x, y: mul(_brcast(g, y), _balanced_eq(x, ans, y)),
           lambda g, ans, x, y: mul(_brcast(g, x), _balanced_eq(y, ans, x)))


shift_left_p = standard_binop([_int, _int], 'shift_left')
ad.defjvp_zero(shift_left_p)

shift_right_arithmetic_p = standard_binop([_int, _int], 'shift_right_arithmetic')
ad.defjvp_zero(shift_right_arithmetic_p)

shift_right_logical_p = standard_binop([_int, _int], 'shift_right_logical')
ad.defjvp_zero(shift_right_logical_p)

eq_p = binop(_fixed_dtype(onp.bool_), [_any, _any], 'eq')
ad.defjvp_zero(eq_p)

ne_p = binop(_fixed_dtype(onp.bool_), [_any, _any], 'ne')
ad.defjvp_zero(ne_p)

ge_p = binop(_fixed_dtype(onp.bool_), [_any, _any], 'ge')
ad.defjvp_zero(ge_p)

gt_p = binop(_fixed_dtype(onp.bool_), [_any, _any], 'gt')
ad.defjvp_zero(gt_p)

le_p = binop(_fixed_dtype(onp.bool_), [_any, _any], 'le')
ad.defjvp_zero(le_p)

lt_p = binop(_fixed_dtype(onp.bool_), [_any, _any], 'lt')
ad.defjvp_zero(lt_p)


def _convert_element_type_shape_rule(operand, new_dtype, old_dtype):
  return operand.shape

def _convert_element_type_dtype_rule(operand, new_dtype, old_dtype):
  return new_dtype

def _convert_element_type_translation_rule(c, operand, new_dtype, old_dtype):
  new_etype = xla_client.dtype_to_etype(new_dtype)
  return c.ConvertElementType(operand, new_element_type=new_etype)

convert_element_type_p = standard_primitive(
    _convert_element_type_shape_rule, _convert_element_type_dtype_rule,
    'convert_element_type', _convert_element_type_translation_rule)
ad.deflinear(
    convert_element_type_p,
    lambda t, new_dtype, old_dtype: [convert_element_type(t, old_dtype)])
batching.defvectorized(convert_element_type_p)
masking.defvectorized(convert_element_type_p)


def _bitcast_convert_type_shape_rule(operand, new_dtype):
  return operand.shape

def _bitcast_convert_type_dtype_rule(operand, new_dtype):
  return new_dtype

def _bitcast_convert_type_translation_rule(c, operand, new_dtype):
  new_etype = xla_bridge.dtype_to_etype(new_dtype)
  return c.BitcastConvertType(operand, new_element_type=new_etype)

bitcast_convert_type_p = standard_primitive(
    _bitcast_convert_type_shape_rule, _bitcast_convert_type_dtype_rule,
    'bitcast_convert_type', _bitcast_convert_type_translation_rule)
ad.defjvp_zero(bitcast_convert_type_p)
batching.defvectorized(bitcast_convert_type_p)
masking.defvectorized(bitcast_convert_type_p)


def _conv_general_dilated_shape_rule(
    lhs, rhs, window_strides, padding, lhs_dilation, rhs_dilation,
    dimension_numbers, feature_group_count, **unused_kwargs):
  assert type(dimension_numbers) is ConvDimensionNumbers
  if not feature_group_count > 0:
    msg = ("conv_general_dilated feature_group_count "
           "must be a positive integer, got {}.")
    raise ValueError(msg.format(feature_group_count))
  lhs_feature_count = lhs.shape[dimension_numbers.lhs_spec[1]]
  quot, rem = divmod(lhs_feature_count, feature_group_count)
  if rem:
    msg = ("conv_general_dilated feature_group_count must divide lhs feature "
           "dimension size, but {} does not divide {}.")
    raise ValueError(msg.format(feature_group_count, lhs_feature_count))
  if quot != rhs.shape[dimension_numbers.rhs_spec[1]]:
    msg = ("conv_general_dilated lhs feature dimension size divided by "
           "feature_group_count must equal the rhs input feature dimension "
           "size, but {} // {} != {}.")
    raise ValueError(msg.format(lhs_feature_count, feature_group_count,
                                rhs.shape[dimension_numbers.rhs_spec[1]]))
  if rhs.shape[dimension_numbers.rhs_spec[0]] % feature_group_count:
    msg = ("conv_general_dilated rhs output feature dimension size must be a "
           "multiple of feature_group_count, but {} is not a multiple of {}.")
    raise ValueError(msg.format(rhs.shape[dimension_numbers.rhs_spec[0]],
                                feature_group_count))
  lhs_perm, rhs_perm, out_perm = dimension_numbers
  lhs_trans = _dilate_shape(onp.take(lhs.shape, lhs_perm), lhs_dilation)
  rhs_trans = _dilate_shape(onp.take(rhs.shape, rhs_perm), rhs_dilation)
  out_trans = conv_shape_tuple(lhs_trans, rhs_trans, window_strides, padding)
  return tuple(onp.take(out_trans, onp.argsort(out_perm)))

def _conv_general_dilated_dtype_rule(
    lhs, rhs, window_strides, padding, lhs_dilation, rhs_dilation,
    dimension_numbers, **unused_kwargs):
  return binop_dtype_rule(_input_dtype, [_float, _float],
                          'conv_general_dilated', lhs, rhs)

_conv_spec_transpose = lambda spec: (spec[1], spec[0]) + spec[2:]
_conv_sdims = lambda spec: spec[2:]

def _conv_general_dilated_transpose_lhs(
    g, rhs, window_strides, padding, lhs_dilation, rhs_dilation,
    dimension_numbers, feature_group_count,
    lhs_shape, rhs_shape, precision):
  assert type(dimension_numbers) is ConvDimensionNumbers
  lhs_sdims, rhs_sdims, out_sdims = map(_conv_sdims, dimension_numbers)
  lhs_spec, rhs_spec, out_spec = dimension_numbers
  t_rhs_spec = _conv_spec_transpose(rhs_spec)
  if feature_group_count > 1:
    # in addition to switching the dims in the spec, need to move the feature
    # group axis into the transposed rhs's output feature dim
    rhs = _reshape_axis_out_of(rhs_spec[0], feature_group_count, rhs)
    rhs = _reshape_axis_into(rhs_spec[0], rhs_spec[1], rhs)
  trans_dimension_numbers = ConvDimensionNumbers(out_spec, t_rhs_spec, lhs_spec)
  padding = _conv_general_vjp_lhs_padding(
      onp.take(lhs_shape, lhs_sdims), onp.take(rhs_shape, rhs_sdims),
      window_strides, onp.take(g.shape, out_sdims), padding, lhs_dilation,
      rhs_dilation)
  revd_weights = rev(rhs, rhs_sdims)
  return conv_general_dilated(
      g, revd_weights, window_strides=lhs_dilation, padding=padding,
      lhs_dilation=window_strides, rhs_dilation=rhs_dilation,
      dimension_numbers=trans_dimension_numbers,
      feature_group_count=feature_group_count, precision=precision)

def _conv_general_dilated_transpose_rhs(
    g, lhs, window_strides, padding, lhs_dilation, rhs_dilation,
    dimension_numbers, feature_group_count,
    lhs_shape, rhs_shape, precision):
  assert type(dimension_numbers) is ConvDimensionNumbers

  lhs_sdims, rhs_sdims, out_sdims = map(_conv_sdims, dimension_numbers)
  lhs_trans, rhs_trans, out_trans = map(_conv_spec_transpose, dimension_numbers)
  if feature_group_count > 1:
    lhs = _reshape_axis_out_of(lhs_trans[0], feature_group_count, lhs)
    lhs = _reshape_axis_into(lhs_trans[0], lhs_trans[1], lhs)
  trans_dimension_numbers = ConvDimensionNumbers(lhs_trans, out_trans, rhs_trans)
  padding = _conv_general_vjp_rhs_padding(
      onp.take(lhs_shape, lhs_sdims), onp.take(rhs_shape, rhs_sdims),
      window_strides, onp.take(g.shape, out_sdims), padding, lhs_dilation,
      rhs_dilation)
  return conv_general_dilated(
      lhs, g, window_strides=rhs_dilation, padding=padding,
      lhs_dilation=lhs_dilation, rhs_dilation=window_strides,
      dimension_numbers=trans_dimension_numbers,
      feature_group_count=feature_group_count, precision=precision)

def _conv_general_dilated_translation_rule(
    c, lhs, rhs, window_strides, padding, lhs_dilation, rhs_dilation,
    dimension_numbers, feature_group_count, precision, **unused_kwargs):
  assert type(dimension_numbers) is ConvDimensionNumbers
  dimension_numbers = _conv_general_proto(dimension_numbers)
  return c.ConvGeneralDilated(lhs, rhs, window_strides, padding, lhs_dilation,
                              rhs_dilation, dimension_numbers,
                              feature_group_count,
                              precision_config=_precision_config(precision))

def _conv_general_dilated_batch_rule(
    batched_args, batch_dims, window_strides, padding,
    lhs_dilation, rhs_dilation, dimension_numbers,
    feature_group_count, precision, **unused_kwargs):
  lhs, rhs = batched_args
  lhs_bdim, rhs_bdim = batch_dims
  lhs_spec, rhs_spec, out_spec = dimension_numbers

  if lhs_bdim is not None and rhs_bdim is not None:
    assert lhs.shape[lhs_bdim] == rhs.shape[rhs_bdim]
    new_lhs = _reshape_axis_into(lhs_bdim, lhs_spec[1], lhs)
    new_rhs = _reshape_axis_into(rhs_bdim, rhs_spec[0], rhs)
    out = conv_general_dilated(
      new_lhs, new_rhs, window_strides, padding, lhs_dilation, rhs_dilation,
      dimension_numbers,
      feature_group_count=lhs.shape[lhs_bdim] * feature_group_count,
      precision=precision)
    out = _reshape_axis_out_of(out_spec[1], lhs.shape[lhs_bdim], out)
    return out, out_spec[1]

  elif lhs_bdim is not None:
    new_lhs = _reshape_axis_into(lhs_bdim, lhs_spec[0], lhs)
    out = conv_general_dilated(new_lhs, rhs, window_strides, padding,
                               lhs_dilation, rhs_dilation, dimension_numbers,
                               feature_group_count, precision=precision)
    out = _reshape_axis_out_of(out_spec[0], lhs.shape[lhs_bdim], out)
    return out, out_spec[0]

  elif rhs_bdim is not None:
    if feature_group_count == 1:
      new_rhs = _reshape_axis_into(rhs_bdim, rhs_spec[0], rhs)
      out = conv_general_dilated(lhs, new_rhs, window_strides, padding,
                                lhs_dilation, rhs_dilation, dimension_numbers,
                                feature_group_count, precision=precision)
      out = _reshape_axis_out_of(out_spec[1], rhs.shape[rhs_bdim], out)
      return out, out_spec[1]
    else:
      # feature_group needs to be outermost, so we need to factor it out of the
      # rhs output feature dim, then factor the batch dim into the remaining rhs
      # output feature dim, then put feature_group back in. we do something
      # similar on the output. an alternative which would require more FLOPs but
      # fewer reshapes would be to broadcast lhs.
      new_rhs = _reshape_axis_out_of(rhs_spec[0] + int(rhs_bdim <= rhs_spec[0]),
                                     feature_group_count, rhs)
      new_rhs = _reshape_axis_into(rhs_bdim + int(rhs_spec[0] < rhs_bdim),
                                   rhs_spec[0] + 1,
                                   new_rhs)
      new_rhs = _reshape_axis_into(rhs_spec[0], rhs_spec[0], new_rhs)
      out = conv_general_dilated(lhs, new_rhs, window_strides, padding,
                                lhs_dilation, rhs_dilation, dimension_numbers,
                                feature_group_count, precision=precision)
      out = _reshape_axis_out_of(out_spec[1], feature_group_count, out)
      out = _reshape_axis_out_of(out_spec[1] + 1, rhs.shape[rhs_bdim], out)
      out = _reshape_axis_into(out_spec[1], out_spec[1] + 1, out)
      return out, out_spec[1]

conv_general_dilated_p = standard_primitive(
    _conv_general_dilated_shape_rule, _conv_general_dilated_dtype_rule,
    'conv_general_dilated', _conv_general_dilated_translation_rule)
ad.defbilinear(conv_general_dilated_p,
               _conv_general_dilated_transpose_lhs,
               _conv_general_dilated_transpose_rhs)
batching.primitive_batchers[conv_general_dilated_p] = \
    _conv_general_dilated_batch_rule


def _reshape_axis_into(src, dst, x):
  perm = [i for i in range(x.ndim) if i != src]
  perm.insert(dst, src)
  new_shape = list(onp.delete(x.shape, src))
  new_shape[dst] *= x.shape[src]
  return reshape(x, new_shape, perm)

def _reshape_axis_out_of(src, size1, x):
  shape = list(x.shape)
  size2, ragged = divmod(shape[src], size1)
  assert not ragged
  shape[src:src+1] = [size1, size2]
  return reshape(x, shape)


def _dot_shape_rule(lhs, rhs, precision):
  if lhs.ndim == 0 or rhs.ndim == 0:
    msg = "Dot only supports rank 1 or above, got shapes {} and {}."
    raise TypeError(msg.format(lhs.shape, rhs.shape))
  if lhs.ndim > 2 or rhs.ndim > 2:
    msg = "Dot only supports rank 2 or less, got shapes {} and {}."
    raise TypeError(msg.format(lhs.shape, rhs.shape))

  def require(shape_cond):
    if not shape_cond:
      msg = "Incompatible shapes for dot: got {} and {}."
      raise TypeError(msg.format(lhs.shape, rhs.shape))

  if lhs.ndim == rhs.ndim == 1:
    require(lhs.shape == rhs.shape)
    return ()
  elif lhs.ndim == rhs.ndim == 2:
    require(lhs.shape[1] == rhs.shape[0])
    return (lhs.shape[0], rhs.shape[1])
  elif rhs.ndim == 1:
    require(lhs.shape[-1] == rhs.shape[0])
    return lhs.shape[:-1]
  else:
    require(lhs.shape[-1] == rhs.shape[-2])
    return lhs.shape[:-1] + rhs.shape[:-2] + rhs.shape[-1:]

def _dot_transpose_lhs(t, rhs, precision):
  if onp.ndim(t) == onp.ndim(rhs) == 2:
    return dot(t, transpose(rhs, (1, 0)), precision=precision)
  elif onp.ndim(t) == 1 and onp.ndim(rhs) == 2:
    return dot(rhs, t, precision=precision)
  elif onp.ndim(t) == onp.ndim(rhs) == 1:
    return _outer(t, rhs)
  elif onp.ndim(t) == 0 or onp.ndim(rhs) == 0:
    return mul(t, rhs)
  else:
    raise TypeError

def _dot_transpose_rhs(t, lhs, precision):
  if onp.ndim(lhs) == onp.ndim(t) == 2:
    return dot(transpose(lhs, (1, 0)), t)
  elif onp.ndim(lhs) == 2 and onp.ndim(t) == 1:
    return dot(t, lhs, precision=precision)
  elif onp.ndim(t) == onp.ndim(lhs) == 1:
    return _outer(lhs, t)
  elif onp.ndim(t) == 0 or onp.ndim(lhs) == 0:
    return mul(t, lhs)
  else:
    raise TypeError

def _outer(x, y):
  assert onp.ndim(x) == onp.ndim(y) == 1
  return mul(reshape(x, (x.shape[0], 1)), reshape(y, (1, y.shape[0])))

def _dot_batch_rule(batched_args, batch_dims, precision=None):
  lhs, rhs = batched_args
  lbd, rbd = batch_dims
  T = lambda x: transpose(x, onp.arange(onp.ndim(x))[::-1])

  # in some cases, we can call dot instead of dot_general
  if max(onp.ndim(lhs), onp.ndim(rhs)) <= 2:
    if rbd is None:
      assert lbd in (0, 1)
      if lbd == 0:
        return dot(lhs, rhs, precision=precision), 0
      else:
        return dot(T(rhs), lhs, precision=precision), onp.ndim(rhs) - 1

    if lbd is None:
      assert rbd in (0, 1)
      if rbd == onp.ndim(rhs) - 1:
        return dot(lhs, rhs, precision=precision), onp.ndim(lhs) - 1
      else:
        return dot(rhs, T(lhs), precision=precision), 0

    assert lbd is not None and rbd is not None
    assert lhs.ndim == rhs.ndim == 2  # dot only supports rank 1 and above
    lhs = batching.moveaxis(lhs, lbd, 0)
    rhs = batching.moveaxis(rhs, rbd, 0)
    out = dot_general(lhs, rhs, [((1,), (1,)), ((0,), (0,))],
                      precision=precision)
    return out, 0

  if lbd is None:
    assert rbd is not None
    lhs = broadcast(lhs, (rhs.shape[rbd],))
  else:
    lhs = batching.moveaxis(lhs, lbd, 0)
  lhs_batch = (0,)
  lhs_contracting = (onp.ndim(lhs) - 1,)

  if rbd is None:
    assert lbd is not None
    rhs = broadcast(rhs, (lhs.shape[0],))
  else:
    rhs = batching.moveaxis(rhs, rbd, 0)
  rhs_batch = (0,)
  rhs_contracting = (onp.arange(1, onp.ndim(rhs))[-2:][0],)

  dim_nums = [(lhs_contracting, rhs_contracting), (lhs_batch, rhs_batch)]
  return dot_general(lhs, rhs, dim_nums, precision=precision), 0

def _precision_config(precision):
  if precision is not None:
    config = xla_client.PrecisionConfig()
    config.operand_precision.extend((precision, precision))
    return config
  return None

def _dot_translation_rule(c, lhs, rhs, precision):
  return c.Dot(lhs, rhs, precision_config=_precision_config(precision))

def _dot_polymorphic_shape_rule(shape_exprs, precision):
  del precision  # Unused.
  lhs_shape, rhs_shape = shape_exprs
  lhs_ndim, rhs_ndim = len(lhs_shape), len(rhs_shape)

  if lhs_ndim == rhs_ndim == 1:
    if not lhs_shape == rhs_shape: raise ShapeError
    return ShapeExpr(())
  elif lhs_ndim == rhs_ndim == 2:
    if not lhs_shape[1] == rhs_shape[0]: raise ShapeError
    return ShapeExpr((lhs_shape[0], rhs_shape[1]))
  elif rhs_ndim == 1:
    if not lhs_shape[1] == rhs_shape[0]: raise ShapeError
    return ShapeExpr((lhs_shape[0],))
  else:
    if not lhs_shape[0] == rhs_shape[0]: raise ShapeError
    return ShapeExpr((rhs_shape[1],))

def _dot_masking_rule(padded_vals, logical_shapes, precision):
  lhs, rhs = padded_vals
  lhs_shape, rhs_shape = logical_shapes
  lhs_ndim, rhs_ndim = len(lhs_shape), len(rhs_shape)

  if lhs_ndim == rhs_ndim == 1:
    masked_lhs = select(iota(onp.int32, lhs.shape[0]) < lhs_shape[0],
                        lhs, zeros_like_array(lhs))
    return dot_p.bind(masked_lhs, rhs, precision=precision)
  elif lhs_ndim == rhs_ndim == 2:
    # TODO could avoid select if we check whether contracted axis is masked
    masked_lhs = select(broadcasted_iota(onp.int32, lhs.shape, 1) < lhs_shape[1],
                        lhs, zeros_like_array(lhs))
    return dot_p.bind(masked_lhs, rhs, precision=precision)
  elif rhs_ndim == 1:
    raise NotImplementedError
  else:
    raise NotImplementedError


_dot_dtype_rule = partial(binop_dtype_rule, _input_dtype, [_num, _num], 'dot')
dot_p = standard_primitive(_dot_shape_rule, _dot_dtype_rule, 'dot',
                           _dot_translation_rule)
ad.defbilinear(dot_p, _dot_transpose_lhs, _dot_transpose_rhs)
batching.primitive_batchers[dot_p] = _dot_batch_rule
masking.shape_rules[dot_p] = _dot_polymorphic_shape_rule
masking.masking_rules[dot_p] = _dot_masking_rule


def _dot_general_shape_rule(lhs, rhs, dimension_numbers, precision):
  (lhs_contracting, rhs_contracting), (lhs_batch, rhs_batch) = dimension_numbers
  if len(lhs_batch) != len(rhs_batch):
    msg = ("dot_general requires equal numbers of lhs_batch and rhs_batch "
           "dimensions, got lhs_batch {} and rhs_batch {}.")
    raise TypeError(msg.format(lhs_batch, rhs_batch))
  if not onp.all(onp.equal(lhs_batch, rhs_batch)):
    msg = ("dot_general requires same lhs and rhs batch dimension numbers, "
           "got {} and {}.")
    raise TypeError(msg.format(lhs_batch, rhs_batch))
  lhs_batch_shape = onp.take(lhs.shape, lhs_batch)
  rhs_batch_shape = onp.take(rhs.shape, rhs_batch)
  if not onp.all(onp.equal(lhs_batch_shape, rhs_batch_shape)):
    msg = ("dot_general requires lhs batch dimensions and rhs batch dimensions "
           "to have the same shape, got {} and {}.")
    raise TypeError(msg.format(lhs_batch_shape, rhs_batch_shape))
  if tuple(sorted(lhs_batch)) != tuple(range(len(lhs_batch))):
    msg = ("dot_general requires lhs batch dimensions to precede contracting "
           "and non-contracting dimensions, got lhs_batch {}.")
    raise TypeError(msg.format(lhs_batch))
  if tuple(sorted(rhs_batch)) != tuple(range(len(rhs_batch))):
    msg = ("dot_general requires rhs batch dimensions to precede contracting "
           "and non-contracting dimensions, got rhs_batch {}.")
    raise TypeError(msg.format(rhs_batch))
  lhs_contracting_shape = onp.take(lhs.shape, lhs_contracting)
  rhs_contracting_shape = onp.take(rhs.shape, rhs_contracting)
  if not onp.all(onp.equal(lhs_contracting_shape, rhs_contracting_shape)):
    msg = ("dot_general requires contracting dimensions to have the same "
           "shape, got {} and {}.")
    raise TypeError(msg.format(lhs_contracting_shape, rhs_contracting_shape))

  batch_shape = tuple(onp.take(lhs.shape, lhs_batch))
  lhs_contract_or_batch = tuple(lhs_contracting) + tuple(lhs_batch)
  lhs_tensored_shape = tuple(onp.delete(lhs.shape, lhs_contract_or_batch))
  rhs_contract_or_batch = tuple(rhs_contracting) + tuple(rhs_batch)
  rhs_tensored_shape = tuple(onp.delete(rhs.shape, rhs_contract_or_batch))
  return batch_shape + lhs_tensored_shape + rhs_tensored_shape


def _dot_general_dtype_rule(lhs, rhs, dimension_numbers, precision):
  return binop_dtype_rule(_input_dtype, [_num, _num], 'dot_general', lhs, rhs)


def _dot_general_transpose_lhs(g, y, dimension_numbers, precision,
                               swap_ans=False):
  (x_contract, y_contract), (x_batch, y_batch) = dimension_numbers
  x_ndim = g.ndim - y.ndim + len(x_batch) + 2 * len(x_contract)
  x_kept = remaining(range(x_ndim), x_contract, x_batch)
  y_kept = remaining(range(y.ndim), y_contract, y_batch)
  if swap_ans:
    ans_batch, ans_y, _ = ranges_like(x_batch, y_kept, x_kept)
  else:
    ans_batch, _, ans_y = ranges_like(x_batch, x_kept, y_kept)
  dims = ((ans_y, y_kept), (ans_batch, y_batch))
  x_contract_sorted_by_y = list(onp.take(x_contract, onp.argsort(y_contract)))
  out_axes = onp.argsort(list(x_batch) + x_kept + x_contract_sorted_by_y)
  return transpose(dot_general(g, y, dims), tuple(out_axes))

def _dot_general_transpose_rhs(g, x, dimension_numbers, precision):
  (x_contract, y_contract), (x_batch, y_batch) = dimension_numbers
  swapped_dimension_numbers = ((y_contract, x_contract), (y_batch, x_batch))
  return _dot_general_transpose_lhs(g, x, swapped_dimension_numbers,
                                    precision, swap_ans=True)


def _dot_general_batch_rule(batched_args, batch_dims, dimension_numbers,
                            precision):
  # there are three kinds of dimensions in a dot_general:
  # - contraction dimensions appear in lhs and rhs but not the result
  # - batch dimensions appear in lhs, rhs, and result
  # - tensor product dimensions appear in the result and one of lhs or rhs
  (lhs_contract, rhs_contract), (lhs_batch, rhs_batch) = dimension_numbers
  lhs, rhs = batched_args
  lbd, rbd = batch_dims
  assert lbd is not None or rbd is not None
  if lbd is not None and rbd is not None:
    # adding a batch dimension
    if lbd != 0:
      lhs = batching.moveaxis(lhs, lbd, 0)
    if rbd != 0:
      rhs = batching.moveaxis(rhs, rbd, 0)
    lhs_batch = (0,) + tuple(onp.add(1, lhs_batch))
    rhs_batch = (0,) + tuple(onp.add(1, rhs_batch))
    lhs_contract = tuple(onp.add(1, lhs_contract))
    rhs_contract = tuple(onp.add(1, rhs_contract))
    result_batch_dim = 0
  else:
    # adding a tensor product dimension
    if lbd is not None:
      # make sure it's the last lhs dimension to avoid changing batch dims
      if lbd != lhs.ndim - 1:
        lhs = batching.moveaxis(lhs, lbd, lhs.ndim - 1)
      # lhs tensor product dims in result come after batch dims
      result_batch_dim = lhs.ndim - len(lhs_contract) - 1
    else:
      # make sure it's the last rhs dimension to avoid changing batch dims
      if rbd != rhs.ndim - 1:
        rhs = batching.moveaxis(rhs, rbd, rhs.ndim - 1)
      # rhs tensor product dims in result come after batch dims + lhs tensor
      # product dims
      result_batch_dim = (lhs.ndim - len(lhs_contract) - len(lhs_batch) +
                          rhs.ndim - len(rhs_contract) - 1)
  new_dimension_numbers = [(lhs_contract, rhs_contract), (lhs_batch, rhs_batch)]
  batched_out = dot_general(lhs, rhs, new_dimension_numbers,
                            precision=precision)
  return batched_out, result_batch_dim

def _dot_general_translation_rule(c, lhs, rhs, dimension_numbers, precision):
  return c.DotGeneral(lhs, rhs, dimension_numbers,
                      precision_config=_precision_config(precision))

dot_general_p = standard_primitive(_dot_general_shape_rule,
                                   _dot_general_dtype_rule, 'dot_general',
                                   _dot_general_translation_rule)
ad.defbilinear(dot_general_p,
               _dot_general_transpose_lhs, _dot_general_transpose_rhs)
batching.primitive_batchers[dot_general_p] = _dot_general_batch_rule


def _broadcast_shape_rule(operand, sizes):
  _check_shapelike('broadcast', 'sizes', sizes)
  return tuple(sizes) + operand.shape

def _broadcast_batch_rule(batched_args, batch_dims, sizes):
  operand, = batched_args
  bdim, = batch_dims
  new_bdim = None if bdim is None else bdim + len(sizes)
  return broadcast(operand, sizes), new_bdim

broadcast_p = standard_primitive(
    _broadcast_shape_rule, _input_dtype, 'broadcast')
ad.deflinear(broadcast_p, lambda t, sizes: [_reduce_sum(t, range(len(sizes)))])
batching.primitive_batchers[broadcast_p] = _broadcast_batch_rule


def _broadcast_in_dim_shape_rule(operand, shape, broadcast_dimensions):
  _check_shapelike('broadcast_in_dim', 'shape', shape)
  _check_shapelike('broadcast_in_dim', 'broadcast_dimensions',
                   broadcast_dimensions)
  if operand.ndim != len(broadcast_dimensions):
    msg = ('broadcast_in_dim broadcast_dimensions must have length equal to '
           'operand ndim, got broadcast_dimensions {} for operand ndim {}.')
    raise TypeError(msg.format(broadcast_dimensions, operand.ndim))
  if not set(broadcast_dimensions).issubset(set(range(len(shape)))):
    msg = ('broadcast_in_dim broadcast_dimensions must be a subset of output '
           'dimensions, got {} for operand ndim {} and shape {}.')
    raise TypeError(msg.format(broadcast_dimensions, operand.ndim, shape))
  return shape

def _broadcast_in_dim_transpose_rule(t, shape, broadcast_dimensions):
  axes = tuple(onp.delete(range(len(shape)), broadcast_dimensions))
  return [_reduce_sum(t, axes)]

def _broadcast_in_dim_batch_rule(batched_args, batch_dims, shape,
                                 broadcast_dimensions):
  operand, = batched_args
  bdim, = batch_dims
  new_operand = batching.moveaxis(operand, bdim, 0)
  new_shape = (operand.shape[bdim],) + shape
  new_broadcast_dimensions = (0,) + tuple(onp.add(1, broadcast_dimensions))
  return broadcast_in_dim(new_operand, new_shape, new_broadcast_dimensions), 0


broadcast_in_dim_p = standard_primitive(
    _broadcast_in_dim_shape_rule, _input_dtype, 'broadcast_in_dim')
ad.deflinear(broadcast_in_dim_p, _broadcast_in_dim_transpose_rule)
batching.primitive_batchers[broadcast_in_dim_p] = _broadcast_in_dim_batch_rule


def _clamp_shape_rule(min, operand, max):
  if min.shape and min.shape != operand.shape:
    m = "clamp requires min.shape == operand.shape or min.shape == (), got {}."
    raise TypeError(m.format(min.shape))
  if max.shape and max.shape != operand.shape:
    m = "clamp requires max.shape == operand.shape or max.shape == (), got {}."
    raise TypeError(m.format(max.shape))
  return operand.shape

_clamp_dtype_rule = partial(binop_dtype_rule, _input_dtype, [_any, _any, _any],
                            'clamp')

clamp_p = standard_primitive(_clamp_shape_rule, _clamp_dtype_rule, 'clamp')
ad.defjvp(clamp_p,
          lambda g, min, operand, max:
          select(bitwise_and(gt(min, operand), lt(min, max)),
                 _brcast(g, operand), _zeros(operand)),
          lambda g, min, operand, max:
          select(bitwise_and(gt(operand, min), lt(operand, max)),
                 g, _zeros(operand)),
          lambda g, min, operand, max:
          select(lt(max, operand), _brcast(g, operand), _zeros(operand)))


def _concatenate_shape_rule(*operands, **kwargs):
  dimension = kwargs.pop('dimension')
  if not operands:
    msg = "concatenate expects at least one operand, got 0."
    raise TypeError(msg)
  if not all(isinstance(operand, UnshapedArray) for operand in operands):
    msg = "All objects to concatenate must be arrays, got {}."
    op = next(op for op in operands if not isinstance(op, UnshapedArray))
    raise TypeError(msg.format(type(op)))
  if len(set(operand.ndim for operand in operands)) != 1:
    msg = "Cannot concatenate arrays with different ranks, got {}."
    raise TypeError(msg.format(", ".join(str(o.ndim) for o in operands)))
  shapes = onp.array([operand.shape for operand in operands])
  if not 0 <= dimension < shapes.shape[1]:
    msg = "concatenate dimension out of bounds: dimension {} for shapes {}."
    raise TypeError(msg.format(dimension, ", ".join(map(str, shapes))))
  if not onp.all(onp.delete(shapes[0] == shapes, dimension, axis=1)):
    msg = ("Cannot concatenate arrays with shapes that differ in dimensions "
           "other than the one being concatenated: dimension {} for shapes {}.")
    raise TypeError(msg.format(dimension, ", ".join(map(str, shapes))))

  concat_size = sum(o.shape[dimension] for o in operands)
  ex_shape = operands[0].shape
  return ex_shape[:dimension] + (concat_size,) + ex_shape[dimension+1:]

def _concatenate_dtype_rule(*operands, **kwargs):
  _check_same_dtypes('concatenate', False, *(o.dtype for o in operands))
  return operands[0].dtype

def _concatenate_translation_rule(c, *operands, **kwargs):
  dimension = kwargs.pop('dimension')
  return c.Concatenate(operands, dimension=dimension)

def _concatenate_transpose_rule(t, *operands, **kwargs):
  dimension = kwargs.pop('dimension')
  operand_shapes = kwargs.pop('operand_shapes')

  if t is ad_util.zero:
    return [ad_util.zero if o is ad.undefined_primal else None for o in operands]
  else:
    limit_points = onp.cumsum([shape[dimension] for shape in operand_shapes])
    starts = onp.zeros((len(operands), t.ndim), dtype=int)
    starts[1:, dimension] = limit_points[:-1]
    limits = onp.tile(t.shape, (len(operands), 1))
    limits[:, dimension] = limit_points

    return [slice(t, start, limit) if o is ad.undefined_primal else None
            for o, start, limit in zip(operands, starts, limits)]

def _concatenate_batch_rule(batched_args, batch_dims, dimension, operand_shapes):
  size = next(op.shape[bdim] for op, bdim in zip(batched_args, batch_dims)
              if bdim is not None)
  operands = [batching.moveaxis(op, bdim, 0) if bdim is not None
              else broadcast(op, (size,))
              for op, bdim in zip(batched_args, batch_dims)]
  return concatenate(operands, dimension + 1), 0

def _concat_polymorphic_shape_rule(shape_exprs, dimension, operand_shapes):
  out_shape = list(shape_exprs[0])
  out_shape[dimension] = _reduce(operator.add, [e[dimension] for e in shape_exprs])
  return ShapeExpr(out_shape)

# The concatenate_p masking rule requires use of a while-loop construct and so
# is defined in lax_control_flow.py

concatenate_p = standard_primitive(
    _concatenate_shape_rule, _concatenate_dtype_rule, 'concatenate',
    _concatenate_translation_rule)
ad.deflinear(concatenate_p, _concatenate_transpose_rule)
ad.primitive_transposes[concatenate_p] = _concatenate_transpose_rule
batching.primitive_batchers[concatenate_p] = _concatenate_batch_rule
masking.shape_rules[concatenate_p] = _concat_polymorphic_shape_rule


def _pad_shape_rule(operand, padding_value, padding_config):
  if operand.dtype != padding_value.dtype:
    msg = "pad operand and padding_value must be same dtype: got {} and {}."
    raise TypeError(msg.format(operand.dtype, padding_value.dtype))

  lo, hi, interior = zip(*padding_config)
  out_shape = onp.add(onp.add(onp.add(lo, hi), operand.shape),
                      onp.multiply(interior, onp.subtract(operand.shape, 1)))
  return tuple(out_shape)

def _pad_transpose(t, operand, padding_value, padding_config):
  if t is ad_util.zero:
    return [ad_util.zero if operand is ad.undefined_primal else None,
            ad_util.zero if padding_value is ad.undefined_primal else None]

  lo, hi, interior = zip(*padding_config)
  total = lambda x: _reduce_sum(x, list(range(t.ndim)))

  def t_op():
    unpad_config = zip(onp.negative(lo), onp.negative(hi), onp.zeros_like(interior))
    unpadded = pad(t, onp.array(0., t.dtype), unpad_config)
    return slice(unpadded, onp.zeros_like(lo), unpadded.shape, onp.add(interior, 1))

  t_operand = t_op() if operand is ad.undefined_primal else None
  t_padv = sub(total(t), total(t_operand)) if padding_value is ad.undefined_primal else None

  return [t_operand, t_padv]

def _pad_batch_rule(batched_args, batch_dims, padding_config):
  operand, padding_value = batched_args
  operand_bdim, padding_value_bdim = batch_dims
  if padding_value_bdim is None:
    assert operand_bdim is not None
    padding_config = list(padding_config)
    padding_config.insert(operand_bdim, (0, 0, 0))
    return pad(operand, padding_value, padding_config), operand_bdim
  else:
    raise NotImplementedError  # loop and stack

pad_p = standard_primitive(_pad_shape_rule, _input_dtype, 'pad')
ad.deflinear(pad_p, _pad_transpose)
ad.primitive_transposes[pad_p] = _pad_transpose
batching.primitive_batchers[pad_p] = _pad_batch_rule


# We have a nonstandard reshape impl so that we can be lazy about data movement
# for specific types, particularly ShardedDeviceArrays / ChunkedDeviceArrays
def _reshape_impl(operand, new_sizes, dimensions, old_sizes):
  if (type(operand) is pxla.ShardedDeviceArray and dimensions is None
      and _is_axis_merge(old_sizes, new_sizes)):
    aval = ShapedArray(new_sizes, operand.dtype)
    return pxla.ChunkedDeviceArray(old_sizes[0], aval, operand.device_buffers)
  elif (type(operand) is pxla.ChunkedDeviceArray and dimensions is None
        and _is_axis_split(old_sizes, new_sizes)
        and operand.axis_size == new_sizes[0]):
    aval = ShapedArray(new_sizes, operand.dtype)
    return pxla.ShardedDeviceArray(aval, operand.device_buffers)
  else:
    return xla.apply_primitive(reshape_p, operand, new_sizes=new_sizes,
                               dimensions=dimensions, old_sizes=old_sizes)

def _is_axis_merge(s1, s2):
  return s1[2:] == s2[1:] and s1[0] * s1[1] == s2[0]

def _is_axis_split(s1, s2):
  return _is_axis_merge(s2, s1)

def _reshape_shape_rule(operand, new_sizes, dimensions, **unused_kwargs):
  if not onp.all(onp.greater_equal(new_sizes, 0)):
    msg = 'reshape new_sizes must all be positive, got {}.'
    raise TypeError(msg.format(new_sizes))
  if prod(onp.shape(operand)) != prod(new_sizes):
    msg = 'reshape total size must be unchanged, got new_sizes {} for shape {}.'
    raise TypeError(msg.format(new_sizes, onp.shape(operand)))
  if dimensions is not None:
    if set(dimensions) != set(range(onp.ndim(operand))):
      msg = ('reshape dimensions must be a permutation of operand dimensions, '
             'got dimensions {} for shape {}.')
      raise TypeError(msg.format(dimensions, onp.shape(operand)))
  return tuple(new_sizes)

def _reshape_dtype_rule(operand, new_sizes, dimensions, **unused_kwargs):
  return operand.dtype

def _reshape_translation_rule(c, operand, new_sizes, dimensions, old_sizes):
  del old_sizes  # Unused.
  return c.Reshape(operand, new_sizes=new_sizes, dimensions=dimensions)

def _reshape_transpose_rule(t, new_sizes, dimensions, old_sizes):
  if dimensions is None:
    return [reshape(t, old_sizes)]
  else:
    return [transpose(reshape(t, onp.take(old_sizes, dimensions)),
                      onp.argsort(dimensions))]

def _reshape_batch_rule(batched_args, batch_dims, new_sizes, dimensions, **unused):
  operand, = batched_args
  bdim, = batch_dims
  operand = batching.moveaxis(operand, bdim, 0)
  if dimensions is not None:
    dimensions = (0,) + tuple(onp.add(1, dimensions))
  return reshape(operand, operand.shape[:1] + new_sizes, dimensions), 0

def _reshape_polymorphic_shape_rule(shape_exprs, new_sizes, dimensions, old_sizes):
  if dimensions is not None: raise NotImplementedError
  shape_expr, = shape_exprs
  if masking.prod(shape_expr) != masking.prod(new_sizes): raise ShapeError
  return new_sizes

reshape_p = standard_primitive(_reshape_shape_rule, _reshape_dtype_rule,
                               'reshape', _reshape_translation_rule)
reshape_p.def_impl(_reshape_impl)
ad.deflinear(reshape_p, _reshape_transpose_rule)
batching.primitive_batchers[reshape_p] = _reshape_batch_rule
masking.shape_rules[reshape_p] = _reshape_polymorphic_shape_rule


def _rev_shape_rule(operand, dimensions):
  _check_shapelike('rev', 'dimensions', dimensions)
  if len(set(dimensions)) != len(dimensions):
    msg = 'rev dimensions must be unique, got {}.'
    raise TypeError(msg.format(dimensions))
  if not _max(dimensions) < operand.ndim:
    msg = ('rev dimensions must all be less than operand ndim, got dimensions '
           '{} for operand ndim {}.')
    raise TypeError(msg.format(dimensions, operand.ndim))
  return operand.shape

def _rev_batch_rule(batched_args, batch_dims, dimensions):
  operand, = batched_args
  bdim, = batch_dims
  new_dimensions = [i + 1 if i >= bdim else i for i in dimensions]
  return rev(operand, new_dimensions), bdim

rev_p = standard_primitive(_rev_shape_rule, _input_dtype, 'rev')
ad.deflinear(rev_p, lambda t, dimensions: [rev(t, dimensions)])
batching.primitive_batchers[rev_p] = _rev_batch_rule


def _transpose_shape_rule(operand, permutation):
  if not isinstance(permutation, (tuple, list, onp.ndarray)):
    msg = "transpose permutation must be a tuple/list/ndarray, got {}."
    raise TypeError(msg.format(type(permutation)))
  if tuple(sorted(permutation)) != tuple(range(operand.ndim)):
    msg = ("transpose permutation isn't a permutation of operand dimensions, "
           "got permutation {} for operand shape {}.")
    raise TypeError(msg.format(permutation, operand.shape))
  return tuple(onp.take(operand.shape, permutation))

def _transpose_batch_rule(batched_args, batch_dims, permutation):
  operand, = batched_args
  bdim, = batch_dims
  perm = (bdim,) + tuple(i if i < bdim else i+1 for i in permutation)
  return transpose(operand, perm), 0

transpose_p = standard_primitive(_transpose_shape_rule, _input_dtype,
                                 'transpose')
ad.deflinear(transpose_p,
             lambda t, permutation: [transpose(t, onp.argsort(permutation))])
batching.primitive_batchers[transpose_p] = _transpose_batch_rule


def _select_shape_rule(pred, on_true, on_false):
  if on_true.shape != on_false.shape:
    msg = "select on_true and on_false must have the same shape, got {} and {}."
    raise TypeError(msg.format(on_true.shape, on_false.shape))
  if pred.shape and pred.shape != on_true.shape:
    msg = ("select pred must be scalar or have the same shape as on_true and "
           "on_false, got pred shape {} for on_true and on_false of shape {}.")
    raise TypeError(msg.format(pred.shape, on_true.shape))
  return on_true.shape

def _select_dtype_rule(pred, on_true, on_false):
  _check_same_dtypes("select", False, on_true.dtype, on_false.dtype)
  if not onp.issubdtype(pred.dtype, onp.bool_):
    msg = "select pred must be boolean type, got {}."
    raise TypeError(msg.format(pred.dtype))
  return on_true.dtype

def _select_transpose_rule(t, pred, on_true, on_false):
  assert pred is not ad.undefined_primal
  if t is ad_util.zero:
    return [None,
            ad_util.zero if on_true is ad.undefined_primal else None,
            ad_util.zero if on_false is ad.undefined_primal else None]
  else:
    zeros = full_like(t, 0)
    return [None,
            select(pred, t, zeros) if on_true is ad.undefined_primal else None,
            select(pred, zeros, t) if on_false is ad.undefined_primal else None]

def _select_batch_rule(batched_args, batch_dims, **unused_kwargs):
  pred, on_true, on_false, = batched_args
  pred_bdim, ot_bdim, of_bdim = batch_dims
  size = next(x.shape[i] for x, i in zip(batched_args, batch_dims)
              if i is not None)

  # avoid transposes and some broadcasts in special cases
  if pred_bdim == ot_bdim == of_bdim:
    if onp.shape(pred) == onp.shape(on_true):
      return select(pred, on_true, on_false), pred_bdim
    else:
      # vmapped function had a scalar pred with nonscalar args
      assert onp.ndim(pred) == 1
      pred = broadcast_in_dim(pred, on_true.shape, [pred_bdim])
      return select(pred, on_true, on_false), pred_bdim
  elif onp.ndim(pred) == 0 and ot_bdim is not None and of_bdim is not None:
    if ot_bdim == of_bdim:
      return select(pred, on_true, on_false), ot_bdim
    elif onp.shape(on_true) == onp.shape(on_false):
      on_false = batching.moveaxis(on_false, of_bdim, ot_bdim)
      return select(pred, on_true, on_false), ot_bdim

  pred = batching.bdim_at_front(pred, pred_bdim, size) if onp.shape(pred) else pred
  if not onp.shape(on_true) == onp.shape(on_false) == ():
    on_true = batching.bdim_at_front(on_true, ot_bdim, size)
    on_false = batching.bdim_at_front(on_false, of_bdim, size)
  assert onp.shape(on_true) == onp.shape(on_false)
  if 0 < onp.ndim(pred) < onp.ndim(on_true):
    # vmapped function had a scalar pred with nonscalar args
    assert onp.ndim(pred) == 1
    pred = broadcast_in_dim(pred, on_true.shape, [0])
  if onp.ndim(pred) > onp.ndim(on_true):
    assert onp.ndim(on_true) == 0
    on_true = broadcast(on_true, pred.shape)
    on_false = broadcast(on_false, pred.shape)
  return select(pred, on_true, on_false), 0

select_p = standard_primitive(_select_shape_rule, _select_dtype_rule, 'select')
ad.defjvp(select_p,
          None,
          lambda g, b, x, y: select(b, g, _zeros(g)),
          lambda g, b, x, y: select(b, _zeros(g), g))
ad.primitive_transposes[select_p] = _select_transpose_rule
batching.primitive_batchers[select_p] = _select_batch_rule


def _slice_shape_rule(operand, start_indices, limit_indices, strides,
                      operand_shape):
  _check_shapelike("slice", "start_indices", start_indices)
  _check_shapelike("slice", "limit_indices", limit_indices)
  if operand.ndim != len(start_indices):
    msg = ("slice start_indices must have length equal to the number of "
           "dimensions of the operand, got indices {} for operand shape {}.")
    raise TypeError(msg.format(start_indices, operand.shape))
  if len(start_indices) != len(limit_indices):
    msg = ("slice limit_indices must have the same length as start_indices, "
           "got start_inidices {} and limit_indices {}.")
    raise TypeError(msg.format(start_indices, limit_indices))
  if not onp.all(onp.less_equal(limit_indices, operand.shape)):
    msg = ("slice limit_indices must be less than or equal to operand shape, "
           "got limit_indices {} for operand shape {}.")
    raise TypeError(msg.format(limit_indices, operand.shape))
  if not onp.all(onp.greater_equal(start_indices, 0)):
    msg = ("slice start_indices must be greater than or equal to zero, "
           "got start_indices of {}.")
    raise TypeError(msg.format(start_indices))
  if not onp.all(onp.greater_equal(limit_indices, start_indices)):
    msg = ("slice limit_indices must be greater than or equal to start_indices,"
           " got start_indices {} and limit_indices {}.")
    raise TypeError(msg.format(start_indices, limit_indices))
  if strides is None:
    strides = onp.ones(operand.ndim, onp.int32)
  else:
    _check_shapelike("slice", "strides", strides)
    if len(strides) != operand.ndim:
      msg = ("slice strides must have length equal to the number of dimensions "
             "of the operand, got strides {} for operand shape {}.")
      raise TypeError(msg.format(strides, operand.shape))
    if not onp.all(onp.greater(strides, 0)):
      msg = "slice strides must be positive, got {}"
      raise TypeError(msg.format(strides))

  result_shape = onp.floor_divide(
      onp.add(onp.subtract(limit_indices, start_indices), strides) - 1, strides)
  return tuple(result_shape)

def _slice_translation_rule(c, operand, start_indices, limit_indices, strides,
                            operand_shape):
  return c.Slice(operand, start_indices, limit_indices, strides)

def _slice_transpose_rule(t, start_indices, limit_indices, strides,
                          operand_shape):
  if strides is None or onp.all(onp.equal(strides, 1)):
    pads = zip(start_indices, onp.subtract(operand_shape, limit_indices),
               (0,) * len(start_indices))
  else:
    real_limits = onp.add(onp.add(start_indices, 1),
                          onp.multiply(onp.subtract(t.shape, 1), strides))
    pads = zip(start_indices, onp.subtract(operand_shape, real_limits),
               onp.subtract(strides, 1))
  result = pad(t, _const(t, 0), pads)
  assert result.shape == operand_shape
  return [result]

def _slice_batching_rule(batched_args, batch_dims, start_indices, limit_indices,
                         strides, **unused_kwargs):
  operand, = batched_args
  bdim, = batch_dims

  new_start_indices = list(start_indices)
  new_start_indices.insert(bdim, 0)

  new_limit_indices = list(limit_indices)
  new_limit_indices.insert(bdim, operand.shape[bdim])

  if strides is None:
    new_strides = None
  else:
    new_strides = list(strides)
    new_strides.insert(bdim, 1)

  out = slice(operand, new_start_indices, new_limit_indices, new_strides)
  return out, bdim

slice_p = standard_primitive(_slice_shape_rule, _input_dtype, 'slice',
                             _slice_translation_rule)
ad.deflinear(slice_p, _slice_transpose_rule)
batching.primitive_batchers[slice_p] = _slice_batching_rule


def _dynamic_slice_shape_rule(operand, *start_indices, **kwargs):
  slice_sizes = kwargs["slice_sizes"]
  if operand.ndim != len(start_indices):
    msg = ("dynamic_slice start_indices must have length equal to the number "
           "of dimensions of the operand, got indices {} for operand shape {}.")
    raise TypeError(msg.format(start_indices, operand.shape))
  if len(start_indices) != len(slice_sizes):
    msg = ("dynamic_slice slice_sizes must have the same length as "
           "start_indices, got start_inidices length {} and slice_sizes {}.")
    raise TypeError(msg.format(len(start_indices), slice_sizes))
  if not onp.all(onp.less_equal(slice_sizes, operand.shape)):
    msg = ("slice slice_sizes must be less than or equal to operand shape, "
           "got slice_sizes {} for operand shape {}.")
    raise TypeError(msg.format(slice_sizes, operand.shape))
  if not onp.all(onp.greater_equal(slice_sizes, 0)):
    msg = ("slice slice_sizes must be greater than or equal to zero, "
           "got slice_sizes of {}.")
    raise TypeError(msg.format(slice_sizes))
  return tuple(slice_sizes)

def _dynamic_slice_translation_rule(c, operand, *start_indices, **kwargs):
  slice_sizes = kwargs["slice_sizes"]
  return c.DynamicSlice(operand, start_indices, slice_sizes)

def _dynamic_slice_jvp(primals, tangents, slice_sizes, operand_shape):
  tangent_out = ad_util.zero
  if tangents[0] is not ad_util.zero:
    tangent_out = dynamic_slice(tangents[0], primals[1:], slice_sizes)
  return dynamic_slice(primals[0], primals[1:], slice_sizes), tangent_out

def _dynamic_slice_transpose_rule(t, operand, *start_indices, **kwargs):
  operand_shape = kwargs["operand_shape"]
  assert operand is ad.undefined_primal
  assert all(s is not ad.undefined_primal for s in start_indices)
  zeros = full(operand_shape, tie_in(t, _zero(t)))
  return ([dynamic_update_slice(zeros, t, start_indices)] +
          [None] * len(start_indices))

def _batch_dynamic_slice_indices(indices, bdims):
  size = next((x.shape[i] for x, i in zip(indices, bdims) if i is not None), -1)
  if size < 0:
    return concatenate([reshape(i, [1]) for i in indices], 0), None
  indices = concatenate(
    [broadcast_in_dim(x, (size, 1),
                      broadcast_dimensions=((0,) if i is not None else ()))
     for x, i in zip(indices, bdims)],
    dimension=1)
  return indices, 0

def _dynamic_slice_batching_rule(batched_args, batch_dims, slice_sizes,
                                 operand_shape):
  # A dynamic slice is a special case of gather; we can delegate to the gather
  # batching rule.
  # TODO(phawkins): consider removing dynamic_slice entirely and using gather
  # always.
  dims = tuple(range(len(operand_shape)))
  dnums = GatherDimensionNumbers(offset_dims=dims, collapsed_slice_dims=(),
                                 start_index_map=dims)
  index, index_bdim = _batch_dynamic_slice_indices(batched_args[1:],
                                                   batch_dims[1:])
  return _gather_batching_rule(
    [batched_args[0], index], [batch_dims[0], index_bdim], dnums, slice_sizes,
    operand_shape)


dynamic_slice_p = standard_primitive(
    _dynamic_slice_shape_rule, _input_dtype, 'dynamic_slice',
    _dynamic_slice_translation_rule)
ad.primitive_jvps[dynamic_slice_p] = _dynamic_slice_jvp
ad.primitive_transposes[dynamic_slice_p] = _dynamic_slice_transpose_rule
batching.primitive_batchers[dynamic_slice_p] = _dynamic_slice_batching_rule


def _dynamic_update_slice_shape_rule(operand, update, *start_indices, **kwargs):
  if operand.ndim != update.ndim:
    msg = ("dynamic_update_slice update must have the same rank as operand, "
           "got update shape {} for operand shape {}.")
    raise TypeError(msg.format(update.shape, operand.shape))
  if operand.ndim != len(start_indices):
    msg = ("dynamic_update_slice start_indices must have length equal to the "
           "rank of operand, got indices {} for operand shape {}.")
    raise TypeError(msg.format(start_indices, operand.shape))
  if not onp.all(onp.less_equal(update.shape, operand.shape)):
    msg = ("dynamic_update_slice update shape must be smaller than operand "
           "shape, got update shape {} for operand shape {}.")
    raise TypeError(msg.format(update.shape, operand.shape))
  return operand.shape

def _dynamic_update_slice_dtype_rule(operand, update, *start_indices, **kwargs):
  _check_same_dtypes("dynamic_update_slice", False, operand.dtype, update.dtype)
  return operand.dtype

def _dynamic_update_slice_jvp(primals, tangents, update_shape):
  operand, update = primals[:2]
  start_indices = primals[2:]
  g_operand, g_update = tangents[:2]
  val_out = dynamic_update_slice(operand, update, start_indices)
  if g_operand is ad_util.zero and g_update is ad_util.zero:
    tangent_out = ad_util.zero
  else:
    g_operand = ad.instantiate_zeros(operand, g_operand)
    g_update = ad.instantiate_zeros(update, g_update)
    tangent_out = dynamic_update_slice(g_operand, g_update, start_indices)
  return val_out, tangent_out

def _dynamic_update_slice_transpose_rule(t, operand, update, *start_indices,
                                         **kwargs):
  update_shape = kwargs["update_shape"]
  assert all(x is not ad.undefined_primal for x in start_indices)
  dus = dynamic_update_slice
  ds = dynamic_slice
  zeros = _zeros(t, shape=update_shape)
  operand_t = dus(t, zeros, start_indices) if operand is ad.undefined_primal else None
  update_t = ds(t, start_indices, update_shape) if update is ad.undefined_primal else None
  return [operand_t, update_t] + [None] * len(start_indices)

def _dynamic_update_slice_translation_rule(c, operand, update, *start_indices,
                                           **kwargs):
  return c.DynamicUpdateSlice(operand, update, start_indices)

def _dynamic_update_slice_batching_rule(batched_args, batch_dims, update_shape):
  # A dynamic update slice is a special case of scatter; we can delegate to the
  # scatter batching rule.
  # TODO(phawkins): consider removing dynamic_update_slice entirely and using
  # scatter always.
  operand, update = batched_args[:2]
  operand_bdims, update_bdims = batch_dims[:2]
  dims = tuple(range(len(update_shape)))
  dnums = ScatterDimensionNumbers(update_window_dims=dims,
                                  inserted_window_dims=(),
                                  scatter_dims_to_operand_dims=dims)
  index, index_bdim = _batch_dynamic_slice_indices(batched_args[2:],
                                                   batch_dims[2:])
  return _scatter_batching_rule(
    scatter,
    (operand, index, update), (operand_bdims, index_bdim, update_bdims),
    None, None, dnums, update_shape)


dynamic_update_slice_p = standard_primitive(
    _dynamic_update_slice_shape_rule, _dynamic_update_slice_dtype_rule,
    'dynamic_update_slice', _dynamic_update_slice_translation_rule)
ad.primitive_jvps[dynamic_update_slice_p] = _dynamic_update_slice_jvp
ad.primitive_transposes[dynamic_update_slice_p] = \
    _dynamic_update_slice_transpose_rule
batching.primitive_batchers[dynamic_update_slice_p] = \
    _dynamic_update_slice_batching_rule


class GatherDimensionNumbers(collections.namedtuple(
    "GatherDimensionNumbers",
    ["offset_dims", "collapsed_slice_dims", "start_index_map"])):
  """
  Describes the dimension number arguments to an `XLA's Gather operator
  <https://www.tensorflow.org/xla/operation_semantics#gather>`_. See the XLA
  documentation for more details of what the dimension numbers mean.

  Args:
    offset_dims: the set of dimensions in the `gather` output that offset into
      an array sliced from `operand`. Must be a tuple of integers in ascending
      order, each representing a dimension number of the output.
    collapsed_slice_dims: the set of dimensions `i` in `operand` that have
      `slice_sizes[i] == 1` and that should not have a corresponding dimension
      in the output of the gather. Must be a tuple of integers in ascending
      order.
    start_index_map: for each dimension in `start_indices`, gives the
      corresponding dimension in `operand` that is to be sliced. Must be a
      tuple of integers with size equal to `start_indices.shape[-1]`.

  Unlike XLA's `GatherDimensionNumbers` structure, `index_vector_dim` is
  implicit; there is always an index vector dimension and it must always be the
  last dimension. To gather scalar indices, add a trailing dimension of size 1.
  """

def _gather_dimensions_proto(indices_shape, dimension_numbers):
  assert type(dimension_numbers) is GatherDimensionNumbers
  proto = xla_client.GatherDimensionNumbers()
  proto.offset_dims.extend(dimension_numbers.offset_dims)
  proto.collapsed_slice_dims.extend(dimension_numbers.collapsed_slice_dims)
  proto.start_index_map.extend(dimension_numbers.start_index_map)
  assert indices_shape.rank() > 0
  proto.index_vector_dim = indices_shape.rank() - 1
  return proto

def _gather_dtype_rule(operand, start_indices, **kwargs):
  if not onp.issubdtype(start_indices.dtype, onp.integer):
    raise ValueError("start_indices must have an integer type")
  return xla_bridge.canonicalize_dtype(operand.dtype)

def _gather_shape_rule(operand, start_indices, dimension_numbers, slice_sizes,
                       operand_shape):
  assert operand.shape == operand_shape
  if len(operand_shape) != len(slice_sizes):
    msg = ("slice_sizes must have rank equal to the gather operand; "
          "operand.shape={}, slice_sizes={}".format(operand_shape, slice_sizes))
    raise ValueError(msg)
  result_rank = len(dimension_numbers.offset_dims) + start_indices.ndim - 1
  start_indices_shape = iter(start_indices.shape[:-1])
  slice_sizes = iter(onp.delete(slice_sizes, dimension_numbers.collapsed_slice_dims))
  return tuple(next(slice_sizes) if i in dimension_numbers.offset_dims
               else next(start_indices_shape) for i in range(result_rank))

def _gather_translation_rule(c, operand, start_indices, dimension_numbers,
                             slice_sizes, operand_shape):
  indices_shape = c.GetShape(start_indices)
  return c.Gather(
    operand, start_indices,
    _gather_dimensions_proto(indices_shape, dimension_numbers), slice_sizes)

def _gather_jvp_rule(g, operand, start_indices, dimension_numbers, slice_sizes,
                     operand_shape):
  return gather(g, start_indices, dimension_numbers, slice_sizes)

def _gather_transpose_rule(t, operand, start_indices, dimension_numbers,
                          slice_sizes, operand_shape):
  assert operand is ad.undefined_primal
  if t is ad_util.zero:
    return [ad_util.zero, ad_util.zero]
  zeros = full(operand_shape, tie_in(t, _zero(t)))
  scatter_dnums = ScatterDimensionNumbers(
    update_window_dims=dimension_numbers.offset_dims,
    inserted_window_dims=dimension_numbers.collapsed_slice_dims,
    scatter_dims_to_operand_dims=dimension_numbers.start_index_map)
  return [scatter_add(zeros, start_indices, t, scatter_dnums), ad_util.zero]

def _gather_batching_rule(batched_args, batch_dims, dimension_numbers,
                          slice_sizes, operand_shape):
  operand, start_indices = batched_args
  operand_bdim, start_indices_bdim = batch_dims

  if operand_bdim is not None and start_indices_bdim is None:
    operand = batching.moveaxis(operand, operand_bdim, 0)
    slice_sizes = (operand.shape[0],) + slice_sizes
    offset_dims = (0,) + tuple(onp.add(1, dimension_numbers.offset_dims))
    collapsed_slice_dims = tuple(onp.add(1, dimension_numbers.collapsed_slice_dims))
    start_index_map = tuple(onp.add(1, dimension_numbers.start_index_map))
    dnums = GatherDimensionNumbers(
        offset_dims=offset_dims,
        collapsed_slice_dims=collapsed_slice_dims,
        start_index_map=start_index_map)
    return gather(operand, start_indices, dimension_numbers=dnums,
                  slice_sizes=slice_sizes), 0

  elif operand_bdim is None and start_indices_bdim is not None:
    start_indices = batching.moveaxis(start_indices, start_indices_bdim, 0)
    offset_dims = tuple(onp.add(1, dimension_numbers.offset_dims))
    dnums = GatherDimensionNumbers(
        offset_dims=offset_dims,
        collapsed_slice_dims=dimension_numbers.collapsed_slice_dims,
        start_index_map=dimension_numbers.start_index_map)
    return gather(operand, start_indices, dimension_numbers=dnums,
                  slice_sizes=slice_sizes), 0

  else:
    # move our batch dimensions to the front to preserve sanity
    operand = batching.moveaxis(operand, operand_bdim, 0)
    start_indices = batching.moveaxis(start_indices, start_indices_bdim, 0)

    # Example: user code had start_indices shape (3, 4, 5), and we have to deal
    # with start_indices shape (7, 3, 4, 5). We transform that to a
    # start_indices of shape (7, 3, 4, 6) where we concatenated an iota that
    # counts along our batch dimension to the front of the ndindex.
    count_shape = list(start_indices.shape)
    count_shape[-1] = 1
    counts = broadcasted_iota(start_indices.dtype, tuple(count_shape), 0)
    start_indices = concatenate([counts, start_indices], len(count_shape) - 1)

    slice_sizes = (1,) + slice_sizes
    collapsed_slice_dims = (0,) + tuple(onp.add(1, dimension_numbers.collapsed_slice_dims))
    offset_dims = tuple(onp.add(1, dimension_numbers.offset_dims))
    start_index_map = (0,) + tuple(onp.add(1, dimension_numbers.start_index_map))

    dnums = GatherDimensionNumbers(
        offset_dims=offset_dims,
        collapsed_slice_dims=collapsed_slice_dims,
        start_index_map=start_index_map)
    return gather(operand, start_indices, dimension_numbers=dnums,
                  slice_sizes=slice_sizes), 0

gather_p = standard_primitive(
    _gather_shape_rule, _gather_dtype_rule, 'gather',
    _gather_translation_rule)
ad.defjvp(gather_p, _gather_jvp_rule, None)
ad.primitive_transposes[gather_p] = _gather_transpose_rule
batching.primitive_batchers[gather_p] = _gather_batching_rule


class ScatterDimensionNumbers(collections.namedtuple(
    "ScatterDimensionNumbers",
    ["update_window_dims", "inserted_window_dims",
     "scatter_dims_to_operand_dims"])):
  """
  Describes the dimension number arguments to an `XLA's Scatter operator
  <https://www.tensorflow.org/xla/operation_semantics#scatter>`_. See the XLA
  documentation for more details of what the dimension numbers mean.

  Args:
    update_window_dims: the set of dimensions in the `updates` that are window
      dimensions. Must be a tuple of integers in ascending
      order, each representing a dimension number.
    inserted_window_dims: the set of size 1 window dimensions that must be inserted
      into the shape of `updates`. Must be a tuple of integers in ascending
      order, each representing a dimension number of the output. These are the
      mirror image of `collapsed_slice_dims` in the case of `gather`.
    scatter_dims_to_operand_dims: for each dimension in `scatter_indices`, gives
      the corresponding dimension in `operand`. Must be a sequence of integers
      with size equal to indices.shape[-1].

  Unlike XLA's `ScatterDimensionNumbers` structure, `index_vector_dim` is
  implicit; there is always an index vector dimension and it must always be the
  last dimension. To scatter scalar indices, add a trailing dimension of size 1.
  """

def _scatter_dimensions_proto(indices_shape, dimension_numbers):
  assert type(dimension_numbers) is ScatterDimensionNumbers
  proto = xla_client.ScatterDimensionNumbers()
  proto.update_window_dims.extend(dimension_numbers.update_window_dims)
  proto.inserted_window_dims.extend(dimension_numbers.inserted_window_dims)
  proto.scatter_dims_to_operand_dims.extend(
      dimension_numbers.scatter_dims_to_operand_dims)
  assert indices_shape.rank() > 0
  proto.index_vector_dim = indices_shape.rank() - 1
  return proto

def _scatter_dtype_rule(operand, scatter_indices, updates, **kwargs):
  if not onp.issubdtype(scatter_indices.dtype, onp.integer):
    raise ValueError("scatter_indices must have an integer type")
  _check_same_dtypes("scatter", False, operand.dtype, updates.dtype)
  return xla_bridge.canonicalize_dtype(operand.dtype)

def _scatter_shape_rule(operand, scatter_indices, updates, **kwargs):
  return operand.shape

def _scatter_translation_rule(c, operand, scatter_indices, updates,
                              update_jaxpr, update_consts, dimension_numbers,
                              updates_shape, backend=None):
  dtype = c.GetShape(operand).numpy_dtype()
  init_value = c.Constant(onp.array(0, dtype))
  update_computation = _reduction_computation(
      c, update_jaxpr, backend, update_consts, init_value)
  indices_shape = c.GetShape(scatter_indices)
  return c.Scatter(operand, scatter_indices, updates, update_computation,
                  _scatter_dimensions_proto(indices_shape, dimension_numbers))

def _scatter_add_jvp(primals, tangents, update_jaxpr, update_consts,
                     dimension_numbers, updates_shape):
  operand, scatter_indices, updates = primals
  g_operand, g_scatter_indices, g_updates = tangents
  val_out = scatter_add_p.bind(
      operand, scatter_indices, updates, update_jaxpr=update_jaxpr,
      update_consts=update_consts, dimension_numbers=dimension_numbers,
      updates_shape=updates_shape)
  if g_operand is ad_util.zero and g_updates is ad_util.zero:
    tangent_out = ad_util.zero
  else:
    g_operand = ad.instantiate_zeros(operand, g_operand)
    g_updates = ad.instantiate_zeros(updates, g_updates)
    tangent_out = scatter_add_p.bind(
        g_operand, scatter_indices, g_updates, update_jaxpr=update_jaxpr,
        update_consts=update_consts, dimension_numbers=dimension_numbers,
        updates_shape=updates_shape)
  return val_out, tangent_out

def _scatter_add_transpose_rule(t, operand, scatter_indices, updates,
                                update_jaxpr, update_consts, dimension_numbers,
                                updates_shape):
  assert scatter_indices is not ad.undefined_primal
  if t is ad_util.zero:
    return [ad_util.zero, None, ad_util.zero]

  operand_t = update_t = None
  if operand is ad.undefined_primal:
    operand_t = t

  if updates is ad.undefined_primal:
    gather_dnums = GatherDimensionNumbers(
      offset_dims=dimension_numbers.update_window_dims,
      collapsed_slice_dims=dimension_numbers.inserted_window_dims,
      start_index_map=dimension_numbers.scatter_dims_to_operand_dims)
    slice_sizes = []
    pos = 0
    for i in xrange(len(t.shape)):
      if i in dimension_numbers.inserted_window_dims:
        slice_sizes.append(1)
      else:
        slice_sizes.append(updates_shape[dimension_numbers.update_window_dims[pos]])
        pos += 1
    update_t = gather(t, scatter_indices, dimension_numbers=gather_dnums,
                      slice_sizes=slice_sizes)
  return [operand_t, None, update_t]

def _scatter_batching_rule(
  scatter_op, batched_args, batch_dims, update_jaxpr, update_consts,
  dimension_numbers, updates_shape):
  operand, scatter_indices, updates = batched_args
  operand_bdim, scatter_indices_bdim, updates_bdim = batch_dims
  del update_jaxpr, update_consts, updates_shape  # Unused.

  # move the operand batch dim to the front if it is not None, otherwise create
  # it at the front (so that we can scatter into it)
  size = next(x.shape[ax] for x, ax in zip(batched_args, batch_dims)
              if ax is not None)
  operand = batching.bdim_at_front(operand, operand_bdim, size)
  operand_bdim = 0

  if scatter_indices_bdim is not None and updates_bdim is None:
    updates = broadcast(updates, (size,))
    updates_bdim = 0

  if scatter_indices_bdim is None and updates_bdim is not None:
    updates = batching.moveaxis(updates, updates_bdim, 0)
    inserted_window_dims = tuple(onp.add(1, dimension_numbers.inserted_window_dims))
    update_window_dims = (0,) + tuple(onp.add(1, dimension_numbers.update_window_dims))
    scatter_dims_to_operand_dims = tuple(onp.add(1, dimension_numbers.scatter_dims_to_operand_dims))
    dnums = ScatterDimensionNumbers(
        update_window_dims=update_window_dims,
        inserted_window_dims=inserted_window_dims,
        scatter_dims_to_operand_dims=scatter_dims_to_operand_dims)
    return scatter_op(operand, scatter_indices, updates, dnums), 0
  else:
    # see the third case in _gather_batching_rule for comparison and comments
    scatter_indices = batching.moveaxis(scatter_indices, scatter_indices_bdim, 0)
    updates = batching.moveaxis(updates, updates_bdim, 0)

    count_shape = list(scatter_indices.shape)
    count_shape[-1] = 1
    counts = broadcasted_iota(scatter_indices.dtype, tuple(count_shape), 0)
    scatter_indices = concatenate([counts, scatter_indices],
                                  len(count_shape) - 1)

    update_window_dims = tuple(onp.add(1, dimension_numbers.update_window_dims))
    inserted_window_dims = (0,) + tuple(onp.add(1, dimension_numbers.inserted_window_dims))
    scatter_dims_to_operand_dims = (0,) + tuple(onp.add(1, dimension_numbers.scatter_dims_to_operand_dims))

    dnums = ScatterDimensionNumbers(
        update_window_dims=update_window_dims,
        inserted_window_dims=inserted_window_dims,
        scatter_dims_to_operand_dims=scatter_dims_to_operand_dims)
    return scatter_op(operand, scatter_indices, updates, dnums), 0

scatter_add_p = standard_reduction_primitive(
    _scatter_shape_rule, _scatter_dtype_rule, 'scatter-add',
    _scatter_translation_rule)
ad.primitive_jvps[scatter_add_p] = _scatter_add_jvp
ad.primitive_transposes[scatter_add_p] = _scatter_add_transpose_rule
batching.primitive_batchers[scatter_add_p] = (
  partial(_scatter_batching_rule, scatter_add))

# TODO(jlebar): Add derivatives.
scatter_min_p = standard_reduction_primitive(
    _scatter_shape_rule, _scatter_dtype_rule, 'scatter-min',
    _scatter_translation_rule)
batching.primitive_batchers[scatter_min_p] = (
  partial(_scatter_batching_rule, scatter_min))

# TODO(jlebar): Add derivatives.
scatter_max_p = standard_reduction_primitive(
    _scatter_shape_rule, _scatter_dtype_rule, 'scatter-max',
    _scatter_translation_rule)
batching.primitive_batchers[scatter_max_p] = (
  partial(_scatter_batching_rule, scatter_max))


def _scatter_jvp(primals, tangents, update_jaxpr, update_consts,
                 dimension_numbers, updates_shape):
  operand, scatter_indices, updates = primals
  g_operand, g_scatter_indices, g_updates = tangents
  dnums = dimension_numbers

  if g_operand is ad_util.zero and g_updates is ad_util.zero:
    val_out = scatter_p.bind(
      operand, scatter_indices, updates, update_jaxpr=update_jaxpr,
      update_consts=update_consts, dimension_numbers=dnums,
      updates_shape=updates_shape)
    tangent_out = ad_util.zero
    return val_out, tangent_out

  # If there are overlapping indices in the scatter, it is unspecified which
  # update "wins". So we use the following perhaps surprising scheme:
  # a) attach a positive ID to each update in updates, forming (value, id) pairs
  #    (using a new array dimension because scatter doesn't actually support
  #     pairs).
  # b) perform the scatter, yielding (value, id) updates, which we split apart.
  # c) perform the inverse gather on the ids (similar to
  #    _scatter_add_transpose), and use it to build a mask for the tangent of
  #    `updates`.
  # d) perform a scatter-add on the masked JVP values. A benefit of using
  #    scatter-add here is that we don't need a `scatter` transpose rule.

  # a) add unique positive IDs (iotas) to the updates, and zeros to the operand.
  operand_shape = operand.shape
  updates_shape = updates.shape
  updates_dtype = _dtype(updates)

  new_operand = reshape(operand, (1,) + operand_shape)
  new_operand = pad(new_operand, _zero(operand),
                    ((0, 1, 0),) + tuple((0, 0, 0) for _ in operand_shape))

  ids_shape = onp.array(updates_shape)
  ids_shape[dnums.update_window_dims,] = 1
  num_ids = onp.prod(ids_shape)
  update_ids = add(reshape(iota(updates_dtype, num_ids), ids_shape),
                   _ones(updates))

  # TODO(phawkins): there is a potential bug here if the number of updates
  # is large enough to overflow the number of mantissa bits in a float so IDs
  # end up colliding. We could also utilize the exponent and sign bits, with a
  # little more work.
  assert num_ids < (2 ** onp.finfo(updates_dtype).nmant)

  updates = reshape(updates, (1,) + updates_shape)
  reshaped_update_ids = reshape(update_ids, (1,) + updates_shape)
  updates_and_ids = concatenate((updates, reshaped_update_ids), 0)

  new_dnums = ScatterDimensionNumbers(
    update_window_dims=(0,) + tuple(d + 1 for d in dnums.update_window_dims),
    inserted_window_dims=tuple(d + 1 for d in dnums.inserted_window_dims),
    scatter_dims_to_operand_dims=tuple(d + 1 for d in dnums.scatter_dims_to_operand_dims))
  outputs = scatter_p.bind(
      new_operand, scatter_indices, updates_and_ids, update_jaxpr=update_jaxpr,
      update_consts=update_consts, dimension_numbers=new_dnums,
      updates_shape=updates_shape)
  val_out = index_in_dim(outputs, 0, keepdims=False)
  scattered_ids = index_in_dim(outputs, 1, keepdims=False)

  # b) compute the inverse gather that "undoes" the scatter on the id values.
  gather_dnums = GatherDimensionNumbers(
    offset_dims=dnums.update_window_dims,
    collapsed_slice_dims=dnums.inserted_window_dims,
    start_index_map=dnums.scatter_dims_to_operand_dims)
  slice_sizes = []
  pos = 0
  for i in xrange(len(scattered_ids.shape)):
    if i in dnums.inserted_window_dims:
      slice_sizes.append(1)
    else:
      slice_sizes.append(updates_shape[dnums.update_window_dims[pos]])
      pos += 1
  gathered_update_ids = gather(scattered_ids, scatter_indices,
                         dimension_numbers=gather_dnums,
                         slice_sizes=slice_sizes)

  # c) mask off input JVP elements that do not correspond to a primal output.
  g_operand = ad.instantiate_zeros(operand, g_operand)
  g_updates = ad.instantiate_zeros(updates, g_updates)
  masked_g_operand = select(eq(scattered_ids, _zeros(scattered_ids)),
                            g_operand, _zeros(g_operand))
  masked_g_updates = select(eq(update_ids, gathered_update_ids),
                            g_updates, _zeros(g_updates))

  # d) perform a scatter-add to compute the tangent output.
  tangent_out = scatter_add(masked_g_operand, scatter_indices, masked_g_updates,
                            dimension_numbers=dnums)
  return val_out, tangent_out


scatter_p = standard_reduction_primitive(
    _scatter_shape_rule, _scatter_dtype_rule, 'scatter',
    _scatter_translation_rule)
ad.primitive_jvps[scatter_p] = _scatter_jvp
batching.primitive_batchers[scatter_p] = (
  partial(_scatter_batching_rule, scatter))


def _reduce_shape_rule(operand, init_value, computation, jaxpr, consts, dimensions):
  return tuple(onp.delete(operand.shape, dimensions))

def _reduce_translation_rule(c, operand, init_value, computation, jaxpr, consts, dimensions,
                             backend=None):
  xla_computation = _reduction_computation(c, jaxpr, backend, consts, init_value)
  return c.Reduce(operand, init_value, xla_computation, dimensions)

def _reduce_batch_rule(batched_args, batch_dims, computation, jaxpr, consts, dimensions):
  operand, init_value = batched_args
  operand_bdim, init_value_bdim = batch_dims
  if init_value_bdim is None:
    assert operand_bdim is not None
    new_dimensions = [d + bool(d >= operand_bdim) for d in dimensions]
    new_operand_bdim = operand_bdim - int(onp.sum(onp.less(dimensions, operand_bdim)))
    return reduce(operand, init_value, computation, new_dimensions), new_operand_bdim
  else:
    raise NotImplementedError  # loop and stack

def _reduction_computation(c, jaxpr, backend, consts, init_value):
  shape = c.GetShape(init_value)
  axis_env = xla.AxisEnv()  # no parallel primitives inside reductions
  subc = xla_bridge.make_computation_builder("reduction_computation")
  consts = [subc.ParameterWithShape(const) for const in consts]
  args = [subc.ParameterWithShape(shape), subc.ParameterWithShape(shape)]
  out, = xla.jaxpr_subcomp(subc, jaxpr, backend, axis_env, consts, (), *args)
  return subc.Build(out)

def _masking_defreducer(prim, identity):
  masking.shape_rules[prim] = _reducer_polymorphic_shape_rule
  masking.masking_rules[prim] = partial(_reducer_masking_rule, prim, identity)

def _reducer_polymorphic_shape_rule(shape_exprs, axes, **unused_params):
  shape_expr, = shape_exprs
  return ShapeExpr([d for i, d in enumerate(shape_expr) if i not in axes])

def _reducer_masking_rule(prim, identity, padded_vals, logical_shapes,
                          axes, input_shape):
  del input_shape  # Unused.
  (padded_val,), (logical_shape,) = padded_vals, logical_shapes
  padded_shape = masking.padded_shape_as_value(padded_val.shape)
  masks = [broadcasted_iota(onp.int32, padded_shape, i) < d
           for i, d in enumerate(logical_shape) if i in axes]
  mask = _reduce(operator.and_, masks)
  masked_val = select(mask, padded_val, identity(padded_shape, padded_val.dtype))
  return prim.bind(masked_val, axes=axes, input_shape=padded_shape)

reduce_p = standard_reduction_primitive(_reduce_shape_rule, _input_dtype, 'reduce',
                                        _reduce_translation_rule)
batching.primitive_batchers[reduce_p] = _reduce_batch_rule


def _reduce_sum_shape_rule(operand, axes, input_shape):
  assert operand.shape == input_shape, ('{} != {}'
                                        .format(operand.shape, input_shape))
  return tuple(onp.delete(operand.shape, axes))

def _reduce_sum_translation_rule(c, operand, axes, input_shape):
  dtype = c.GetShape(operand).numpy_dtype()
  scalar = xla_client.Shape.array_shape(dtype, ())
  return c.Reduce(operand, c.Constant(onp.array(0, dtype)),
                  xla.primitive_computation(add_p, scalar, scalar),
                  axes)

def _reduce_sum_transpose_rule(cotangent, input_shape, axes):
  broadcast_dimensions = tuple(onp.delete(onp.arange(len(input_shape)), axes))
  result = broadcast_in_dim(cotangent, input_shape, broadcast_dimensions)
  assert result.shape == input_shape
  return [result]

reduce_sum_p = standard_primitive(_reduce_sum_shape_rule, _input_dtype,
                                  'reduce_sum', _reduce_sum_translation_rule)
ad.deflinear(reduce_sum_p, _reduce_sum_transpose_rule)
batching.defreducer(reduce_sum_p)
_masking_defreducer(reduce_sum_p,
                    lambda shape, dtype: onp.broadcast_to(onp.array(0, dtype), shape))


def _reduce_prod_shape_rule(operand, axes):
  return tuple(onp.delete(operand.shape, axes))

def _reduce_prod_translation_rule(c, operand, axes):
  dtype = c.GetShape(operand).numpy_dtype()
  scalar = xla_client.Shape.array_shape(dtype, ())
  return c.Reduce(operand, c.Constant(onp.array(1, dtype)),
                  xla.primitive_computation(mul_p, scalar, scalar),
                  axes)

def _reduce_prod_jvp_rule(tangent, operand, axes):
  input_shape = onp.array(operand.shape)

  n = onp.prod(input_shape[list(axes)])
  non_axes = onp.delete(onp.arange(len(input_shape)), axes)

  # Move the reduced axes to the front, and flatten them to 1D.
  permutation = axes + tuple(non_axes)
  new_shape = (n,) + tuple(input_shape[non_axes])
  operand = reshape(operand, new_shape, permutation)
  tangent = reshape(tangent, new_shape, permutation)

  one = _const(operand, 1)
  window_dims = [n] + [1] * len(non_axes)
  window_strides = [1] * (len(non_axes) + 1)

  # Form the partial products of all elements to the left and right of each
  # element.
  left_padding = [(n, -1, 0)] + [(0, 0, 0)] * len(non_axes)
  right_padding = [(-1, n, 0)] + [(0, 0, 0)] * len(non_axes)
  left_products = _reduce_window_prod(pad(operand, one, left_padding),
                                      window_dims, window_strides,
                                      xla_client.PaddingType.VALID)
  right_products = _reduce_window_prod(pad(operand, one, right_padding),
                                       window_dims, window_strides,
                                       xla_client.PaddingType.VALID)

  # Multiply partial products with the tangents and sum.
  return _reduce_sum(mul(tangent, mul(left_products, right_products)), (0,))

reduce_prod_p = standard_primitive(_reduce_prod_shape_rule, _input_dtype,
                                   'reduce_prod', _reduce_prod_translation_rule)
ad.defjvp(reduce_prod_p, _reduce_prod_jvp_rule)
batching.defreducer(reduce_prod_p)


def _reduce_chooser_shape_rule(operand, axes):
  return tuple(onp.delete(operand.shape, axes))

def _reduce_chooser_translation_rule(prim, identity, c, operand, axes):
  dtype = c.GetShape(operand).numpy_dtype()
  scalar = xla_client.Shape.array_shape(dtype, ())
  return c.Reduce(operand, c.Constant(identity(dtype)),
                  xla.primitive_computation(prim, scalar, scalar), axes)

def _reduce_chooser_jvp_rule(g, ans, operand, axes):
  # TODO(mattjj): an alternative is to use variadic reduce to compute the chosen
  # locations in a single pass (rather than comparing equality) and use a
  # gather, and/or even push along the chosen elements of g (b/112040122)
  shape = [1 if i in axes else d for i, d in enumerate(operand.shape)]
  location_indicators = convert_element_type(
      _eq_meet(operand, reshape(ans, shape)), g.dtype)
  counts = _reduce_sum(location_indicators, axes)
  return div(_reduce_sum(mul(g, location_indicators), axes), counts)

_reduce_max_translation_rule = partial(_reduce_chooser_translation_rule, max_p,
                                       _get_max_identity)
reduce_max_p = standard_primitive(_reduce_chooser_shape_rule, _input_dtype,
                                  'reduce_max', _reduce_max_translation_rule)
ad.defjvp2(reduce_max_p, _reduce_chooser_jvp_rule)
batching.defreducer(reduce_max_p)


_reduce_min_translation_rule = partial(
    _reduce_chooser_translation_rule, min_p, _get_min_identity)
reduce_min_p = standard_primitive(_reduce_chooser_shape_rule, _input_dtype,
                                  'reduce_min', _reduce_min_translation_rule)
ad.defjvp2(reduce_min_p, _reduce_chooser_jvp_rule)
batching.defreducer(reduce_min_p)


def _reduce_logical_shape_rule(operand, axes):
  if operand.dtype != onp.bool_:
    msg = "logical reduction requires operand dtype bool, got {}."
    raise TypeError(msg.format(operand.dtype))
  return tuple(onp.delete(operand.shape, axes))

def _reduce_logical_translation_rule(prim, identity, c, operand, axes):
  scalar = xla_client.Shape.array_shape(onp.dtype(onp.bool_), ())
  return c.Reduce(operand, c.Constant(identity(onp.bool_)),
                  xla.primitive_computation(prim, scalar, scalar), axes)

_reduce_or_translation_rule = partial(_reduce_logical_translation_rule,
                                      or_p, _get_max_identity)
reduce_or_p = standard_primitive(_reduce_logical_shape_rule, _fixed_dtype(onp.bool_),
                                 'reduce_or', _reduce_or_translation_rule)
batching.defreducer(reduce_or_p)


_reduce_and_translation_rule = partial(_reduce_logical_translation_rule,
                                       and_p, _get_min_identity)
reduce_and_p = standard_primitive(_reduce_logical_shape_rule, _fixed_dtype(onp.bool_),
                                 'reduce_and', _reduce_and_translation_rule)
batching.defreducer(reduce_and_p)

def _reduce_window_shape_rule(operand, init_value, jaxpr, consts,
                              window_dimensions, window_strides, padding):
  if operand.dtype != init_value.dtype:
    msg = ("reduce_window got inconsistent dtypes for operand and init_value: "
           " got operand dtype {} and init_value dtype {}.")
    raise TypeError(msg.format(operand.dtype, init_value.dtype))
  return _common_reduce_window_shape_rule(operand, window_dimensions,
                                         window_strides, padding)

def _reduce_window_translation_rule(c, operand, init_value, jaxpr, consts,
                                    window_dimensions, window_strides, padding, backend=None):
  xla_computation = _reduction_computation(c, jaxpr, backend, consts, init_value)
  return c.ReduceWindow(operand, init_value, xla_computation, window_dimensions,
                        window_strides, padding)

def _generic_reduce_window_batch_rule(
    batched_args, batch_dims, jaxpr, consts, window_dimensions, window_strides,
    padding):
  operand, init = batched_args
  bdim, init_bdim = batch_dims
  if init_bdim is not None:
    raise NotImplementedError("reduce_window batching is not implemented for "
                              "initial values")

  def reduce_window(x, window_dimensions, window_strides, padding):
    return reduce_window_p.bind(
      x, init, jaxpr=jaxpr, consts=consts, window_dimensions=window_dimensions,
      window_strides=window_strides, padding=padding)
  return _reduce_window_batch_rule(reduce_window, (operand,), (bdim,),
                                   window_dimensions, window_strides, padding)


reduce_window_p = standard_reduction_primitive(
    _reduce_window_shape_rule, _input_dtype, 'reduce_window',
    _reduce_window_translation_rule)
batching.primitive_batchers[reduce_window_p] = _generic_reduce_window_batch_rule


def _reduce_window_sum_shape_rule(operand, window_dimensions, window_strides,
                                  padding, input_shape):
  return _common_reduce_window_shape_rule(operand, window_dimensions,
                                         window_strides, padding)

def _reduce_window_sum_translation_rule(c, operand, window_dimensions,
                                        window_strides, padding, input_shape):
  dtype = c.GetShape(operand).numpy_dtype()
  scalar = xla_client.Shape.array_shape(dtype, ())
  return c.ReduceWindow(operand, c.Constant(onp.array(0, dtype)),
                        xla.primitive_computation(add_p, scalar, scalar),
                        window_dimensions, window_strides, padding)

def _reduce_window_sum_transpose_rule(cotangent, window_dimensions,
                                      window_strides, padding, input_shape):
  in_pads = padtype_to_pads(input_shape, window_dimensions, window_strides,
                            padding)
  ones = [1] * len(input_shape)
  pads = _conv_general_vjp_lhs_padding(
      input_shape, window_dimensions, window_strides, cotangent.shape, in_pads,
      ones, ones)
  padding_config = [(lo, hi, stride - 1)
                    for (lo, hi), stride in zip(pads, window_strides)]
  pad_cotangent = pad(cotangent, _zero(cotangent), padding_config)
  result = _reduce_window_sum(pad_cotangent, window_dimensions, ones,
                              xla_client.PaddingType.VALID)
  assert result.shape == input_shape
  return [result]

def _reduce_window_batch_rule(
    reduce_window, batched_args, bdims, window_dimensions, window_strides,
    padding, input_shape=None):
  operand, = batched_args
  bdim, = bdims

  if bdim is not None:
    window_dimensions = \
        window_dimensions[:bdim] + (1,) + window_dimensions[bdim:]
    window_strides = window_strides[:bdim] + (1,) + window_strides[bdim:]

  operand = reduce_window(
      operand, window_dimensions, window_strides, padding)

  return operand, bdim

reduce_window_sum_p = standard_primitive(
    _reduce_window_sum_shape_rule, _input_dtype, 'reduce_window_sum',
    _reduce_window_sum_translation_rule)
ad.deflinear(reduce_window_sum_p, _reduce_window_sum_transpose_rule)
batching.primitive_batchers[reduce_window_sum_p] = partial(
  _reduce_window_batch_rule, _reduce_window_sum)

def _reduce_window_chooser_translation_rule(
    prim, identity, c, operand, window_dimensions, window_strides, padding):
  dtype = c.GetShape(operand).numpy_dtype()
  scalar = xla_client.Shape.array_shape(dtype, ())
  return c.ReduceWindow(operand, c.Constant(identity(dtype)),
                        xla.primitive_computation(prim, scalar, scalar),
                        window_dimensions, window_strides, padding)

def _reduce_window_chooser_jvp_rule(prim, g, operand, window_dimensions,
                                    window_strides, padding):
  assert prim is max_p or prim is min_p
  select_prim = ge_p if prim is max_p else le_p
  return _select_and_gather_add(g, operand, select_prim, window_dimensions,
                                window_strides, padding)


def _common_reduce_window_shape_rule(operand, window_dimensions, window_strides,
                                     padding):
  _check_shapelike("reduce_window", "window_dimensions", window_dimensions)
  _check_shapelike("reduce_window", "window_strides", window_strides)
  if operand.ndim != len(window_dimensions):
    msg = ("reduce_window got the wrong number of window_dimensions for "
           "operand: got operand shape {} with window_dimensions {}.")
    raise TypeError(msg.format(operand.shape, window_dimensions))
  if len(window_strides) != len(window_dimensions):
    msg = ("reduce_window got inconsistent window_strides and "
           "window_dimensions: got window_strides {} and window_dimensions {}.")
    raise TypeError(msg.format(window_strides, window_dimensions))

  return reduce_window_shape_tuple(operand.shape, window_dimensions,
                                   window_strides, padding)

def reduce_window_shape_tuple(operand_shape, window_dimensions, window_strides,
                              padding):
  pads = padtype_to_pads(operand_shape, window_dimensions, window_strides, padding)
  operand_padded = onp.add(operand_shape, onp.add(*zip(*pads)))
  t = onp.floor_divide(
      onp.subtract(operand_padded, window_dimensions), window_strides) + 1
  return tuple(t)

_reduce_window_max_translation_rule = partial(
    _reduce_window_chooser_translation_rule, max_p, _get_max_identity)
reduce_window_max_p = standard_primitive(
    _common_reduce_window_shape_rule, _input_dtype, 'reduce_window_max',
    _reduce_window_max_translation_rule)
ad.defjvp(reduce_window_max_p, partial(_reduce_window_chooser_jvp_rule, max_p))
batching.primitive_batchers[reduce_window_max_p] = partial(
  _reduce_window_batch_rule, _reduce_window_max)

_reduce_window_min_translation_rule = partial(
    _reduce_window_chooser_translation_rule, min_p, _get_min_identity)
reduce_window_min_p = standard_primitive(
    _common_reduce_window_shape_rule, _input_dtype, 'reduce_window_min',
    _reduce_window_min_translation_rule)
ad.defjvp(reduce_window_min_p, partial(_reduce_window_chooser_jvp_rule, min_p))

_reduce_window_min_batch_rule = partial(_reduce_window_batch_rule,
                                        _reduce_window_min)
batching.primitive_batchers[reduce_window_min_p] = partial(
  _reduce_window_batch_rule, _reduce_window_min)


def _select_and_scatter_shape_rule(
    operand, source, init_value, select_jaxpr, select_consts, scatter_jaxpr,
    scatter_consts, window_dimensions, window_strides, padding):
  _check_shapelike("select_and_scatter", "window_dimensions", window_dimensions)
  _check_shapelike("select_and_scatter", "window_strides", window_strides)
  if len(window_dimensions) != len(window_strides):
    msg = ("select_and_scatter got inconsistent window_strides and "
           "window_dimensions: got window_strides {} and window_dimensions {}.")
    raise TypeError(msg.format(window_strides, window_dimensions))
  return operand.shape

def _select_and_scatter_translation(
  c, operand, source, init_value, select_jaxpr, select_consts, scatter_jaxpr,
  scatter_consts, window_dimensions, window_strides, padding, backend=None):
  select = _reduction_computation(c, select_jaxpr, backend, select_consts, init_value)
  scatter = _reduction_computation(c, scatter_jaxpr, backend, scatter_consts, init_value)
  return c.SelectAndScatter(operand, select, window_dimensions, window_strides,
                            padding, source, init_value, scatter)

select_and_scatter_p = standard_reduction_primitive(
    _select_and_scatter_shape_rule, _input_dtype, 'select_and_scatter',
    _select_and_scatter_translation)


def _select_and_scatter_add_shape_rule(
    source, operand, select_prim, window_dimensions, window_strides, padding):
  return operand.shape

def _select_and_scatter_add_translation(
    c, source, operand, select_prim, window_dimensions, window_strides,
    padding):
  dtype = c.GetShape(operand).numpy_dtype()
  scalar = xla_client.Shape.array_shape(dtype, ())
  select = xla.primitive_computation(select_prim, scalar, scalar)
  scatter = xla.primitive_computation(add_p, scalar, scalar)
  zero = c.Constant(onp.array(0, dtype))
  return c.SelectAndScatter(operand, select, window_dimensions, window_strides,
                            padding, source, zero, scatter)

def _select_and_scatter_add_jvp(
    primals, tangents, select_prim, window_dimensions, window_strides,
    padding):
  source, operand = primals
  g_source, g_operand = tangents
  val_out = _select_and_scatter_add(
      source, operand, select_prim, window_dimensions, window_strides,
      padding)
  del g_operand
  if g_source is ad_util.zero:
    tangent_out = ad_util.zero
  else:
    tangent_out = _select_and_scatter_add(
        g_source, operand, select_prim, window_dimensions,
        window_strides, padding)
  return val_out, tangent_out

def _select_and_scatter_add_transpose(
    t, source, operand, select_prim, window_dimensions, window_strides,
    padding):
  assert source is ad.undefined_primal and operand is not ad.undefined_primal
  source_t = _select_and_gather_add(t, operand, select_prim, window_dimensions,
                                    window_strides, padding)
  return [source_t, None]

def _select_and_scatter_add_batch_rule(batched_args, batch_dims, **kwargs):
  source, operand = batched_args
  s_bdims, o_bdims = batch_dims

  if s_bdims is not None and o_bdims is not None:
    #TODO(#212): use a map construct instead of unrolling.
    source = batching.moveaxis(source, s_bdims, 0)
    operand = batching.moveaxis(operand, o_bdims, 0)
    outputs = [
        _select_and_scatter_add(s, o, **kwargs) for s, o in zip(source, operand)]
    outputs = [reshape(out, (1,) + out.shape) for out in outputs]
    outputs = concatenate(outputs, 0)
    return outputs, 0
  elif s_bdims is not None:
    #TODO(#212): use a map construct instead of unrolling.
    source = batching.moveaxis(source, s_bdims, 0)
    outputs = [
        _select_and_scatter_add(s, operand, **kwargs) for s in source]
    outputs = [reshape(out, (1,) + out.shape) for out in outputs]
    outputs = concatenate(outputs, 0)
    return outputs, 0
  elif o_bdims is not None:
    #TODO(#212): use a map construct instead of unrolling.
    operand = batching.moveaxis(operand, o_bdims, 0)
    outputs = [
        _select_and_scatter_add(source, o, **kwargs) for o in operand]
    outputs = [reshape(out, (1,) + out.shape) for out in outputs]
    outputs = concatenate(outputs, 0)
    return outputs, 0

select_and_scatter_add_p = standard_primitive(
    _select_and_scatter_add_shape_rule, _input_dtype, 'select_and_scatter_add',
    _select_and_scatter_add_translation)
ad.primitive_transposes[select_and_scatter_add_p] = \
    _select_and_scatter_add_transpose
ad.primitive_jvps[select_and_scatter_add_p] = _select_and_scatter_add_jvp
batching.primitive_batchers[select_and_scatter_add_p] = \
    _select_and_scatter_add_batch_rule

def _select_and_gather_add_shape_rule(
    tangents, operand, select_prim, window_dimensions, window_strides, padding):
  if tangents.shape != operand.shape:
    msg = ("select_and_gather_add tangents and operand shapes must match, "
           "got {} and {}.")
    raise TypeError(msg.format(tangents.shape, operand.shape))
  return _common_reduce_window_shape_rule(operand, window_dimensions,
                                          window_strides, padding)


_UINT_DTYPES = {
  16: onp.uint16,
  32: onp.uint32,
  64: onp.uint64,
}


def _select_and_gather_add_translation(
    c, tangents, operand, select_prim, window_dimensions, window_strides,
    padding, max_bits=64):
  shape = c.GetShape(operand)
  dtype = shape.numpy_dtype()
  etype = shape.xla_element_type()
  nbits = onp.finfo(dtype).bits

  assert nbits <= max_bits
  double_word_reduction = nbits * 2 <= max_bits

  const = lambda c, dtype, x: c.Constant(onp.array(x, dtype=dtype),
                                         canonicalize_types=False)

  if double_word_reduction:
  # XLA doesn't yet implement ReduceWindow on tuples (Google bug b/73062247), so
  # we implement a pair-wise ReduceWindow by packing two k-bit values into
  # 2k-bit unsigned integer using bit tricks.
    word_dtype = _UINT_DTYPES[nbits]
    double_word_dtype = _UINT_DTYPES[nbits * 2]
    word_type = xla_client.dtype_to_etype(word_dtype)
    double_word_type = xla_client.dtype_to_etype(double_word_dtype)

    # Packs two values into a tuple.
    def pack(a, b):
      a = c.BitcastConvertType(a, word_type)
      b = c.BitcastConvertType(b, word_type)
      a = c.ConvertElementType(a, double_word_type)
      b = c.ConvertElementType(b, double_word_type)
      a = c.ShiftLeft(a, const(c, double_word_dtype, nbits))
      return c.Or(a, b)

    # Unpacks the first element of a tuple.
    def fst(c, t):
      st = c.ShiftRightLogical(t, const(c, double_word_dtype, nbits))
      return c.BitcastConvertType(c.ConvertElementType(st, word_type), etype)

    # Unpacks the second element of a tuple.
    def snd(t):
      return c.BitcastConvertType(c.ConvertElementType(t, word_type), etype)

  else:
    # The double-word trick above only works if we have a sufficiently large
    # type. As an alternative, we can pack two half words into a single word,
    # at the cost of precision.
    # TODO(b/73062247): add support for tuple reductions and remove this case.
    warnings.warn("Using reduced precision for gradient of reduce-window "
                  "min/max operator to work around missing XLA support for "
                  "pair-reductions. This is likely from a second or "
                  "higher derivative of a max-pooling operation.")
    r_nbits = nbits // 2
    # Drop/round the bottom mantissa bits.
    nexp = onp.finfo(dtype).nexp
    nmant = r_nbits - nexp - 1

    double_word_dtype = word_dtype = _UINT_DTYPES[nbits]
    word_type = xla_client.dtype_to_etype(word_dtype)

    # Packs two values into a tuple.
    def pack(a, b):
      a = c.ReducePrecision(a, exponent_bits=nexp, mantissa_bits=nmant)
      b = c.ReducePrecision(b, exponent_bits=nexp, mantissa_bits=nmant)
      a = c.BitcastConvertType(a, word_type)
      b = c.BitcastConvertType(b, word_type)
      b = c.ShiftRightLogical(b, const(c, word_dtype, r_nbits))
      return c.Or(a, b)

    # Unpacks the first element of a tuple.
    def fst(c, t):
      st = c.And(t, const(c, word_dtype, ((1 << r_nbits) - 1) << r_nbits))
      return c.BitcastConvertType(st, etype)

    # Unpacks the second element of a tuple.
    def snd(t):
      return c.BitcastConvertType(c.ShiftLeft(t, const(c, word_dtype, r_nbits)),
                                  etype)

  def reducer():
    c = xla_bridge.make_computation_builder("select_and_gather_pair_reducer")
    x = c.ParameterWithShape(
      xla_client.Shape.array_shape(onp.dtype(double_word_dtype), ()))
    y = c.ParameterWithShape(
      xla_client.Shape.array_shape(onp.dtype(double_word_dtype), ()))
    assert select_prim is ge_p or select_prim is le_p
    which = c.Ge if select_prim is ge_p else c.Le
    c.Select(which(fst(c, x), fst(c, y)), x, y)
    return c.Build()


  assert select_prim is ge_p or select_prim is le_p
  init = -onp.inf if select_prim is ge_p else onp.inf
  out = c.ReduceWindow(pack(operand, tangents),
                       pack(const(c, dtype, init), const(c, dtype, 0)),
                       reducer(), window_dimensions, window_strides,
                       padding)
  return snd(out)

def _select_and_gather_add_jvp(
    primals, tangents, select_prim, window_dimensions, window_strides,
    padding):
  source, operand = primals
  g_source, g_operand = tangents
  val_out = _select_and_gather_add(
      source, operand, select_prim, window_dimensions, window_strides,
      padding)
  del g_operand
  if g_source is ad_util.zero:
    tangent_out = ad_util.zero
  else:
    tangent_out = _select_and_gather_add(
        g_source, operand, select_prim, window_dimensions,
        window_strides, padding)
  return val_out, tangent_out

def _select_and_gather_add_transpose(
    t, tangents, operand, select_prim, window_dimensions, window_strides,
    padding):
  assert tangents is ad.undefined_primal and operand is not ad.undefined_primal
  result = _select_and_scatter_add(t, operand, select_prim, window_dimensions,
                                   window_strides, padding)
  return [result, None]

select_and_gather_add_p = standard_primitive(
    _select_and_gather_add_shape_rule, _input_dtype, 'select_and_gather_add',
    _select_and_gather_add_translation)
ad.primitive_jvps[select_and_gather_add_p] = _select_and_gather_add_jvp
ad.primitive_transposes[select_and_gather_add_p] = \
    _select_and_gather_add_transpose
xla.backend_specific_translations['tpu'][select_and_gather_add_p] = partial(
  _select_and_gather_add_translation,
  max_bits=32)


sort_shape = lambda operand, dimension: operand.shape

def _sort_jvp_rule(g, operand, dimension):
  _, g_out = sort_key_val(operand, g, dimension)
  return g_out

def _sort_batch_rule(batched_args, batch_dims, dimension):
  operand, = batched_args
  bdim, = batch_dims
  dimension = dimension % (operand.ndim - 1)
  new_dimension = dimension + (bdim <= dimension)
  return sort(operand, dimension=new_dimension), bdim

sort_p = standard_primitive(sort_shape, _input_dtype, 'sort')
ad.defjvp(sort_p, _sort_jvp_rule)
batching.primitive_batchers[sort_p] = _sort_batch_rule


def _sort_key_val_abstract_eval(keys, values, dimension):
  return keys, values

def _sort_key_val_jvp(primals, tangents, dimension):
  # NOTE(mattjj): this re-sorts three times, but if we had a variadic
  # sort_key_val, or if we could apply a fixed permutation efficiently, we could
  # implement this jvp rule with a single sort. The apply_permutation primitive
  # would make the jvp (and corresponding transpose rule) faster and easier.
  # This would also be cleaner if we didn't get the sorted keys out.
  # TODO(mattjj): make sort_key_val variadic, no sorted keys out by default
  keys, values = primals
  keys_tangents, values_tangents = tangents

  val_out = sort_key_val(keys, values, dimension)

  if keys_tangents is ad_util.zero:
    keys_tangents_out = ad_util.zero
  else:
    keys_tangents_out = _sort_jvp_rule(keys_tangents, keys, dimension)

  if values_tangents is ad_util.zero:
    values_tangents_out = ad_util.zero
  else:
    values_tangents_out = _sort_jvp_rule(values_tangents, keys, dimension)

  tangents_out = keys_tangents_out, values_tangents_out
  return val_out, tangents_out

def _sort_key_val_transpose_rule(t, keys, values, dimension):
  t_keys, t_values = t
  assert t_keys is ad_util.zero
  iota = broadcasted_iota(onp.int32, keys.shape, dimension % keys.ndim)
  _, perm = sort_key_val(keys, iota)
  keys_result = ad_util.zero if keys is ad.undefined_primal else None
  values_result = sort_key_val(perm, t_values)[1] if values is ad.undefined_primal else None
  return [keys_result, values_result]

def _sort_key_val_batch_rule(batched_args, batch_dims, dimension):
  keys, values = batched_args
  keys_bdim, values_bdim = batch_dims
  assert keys_bdim is not None or values_bdim is not None
  if keys_bdim == values_bdim:
    new_dimension = dimension + (keys_bdim <= dimension)
    return sort_key_val(keys, values, new_dimension), (keys_bdim, keys_bdim)
  elif keys_bdim is not None and values_bdim is not None:
    keys_trans = batching.moveaxis(keys, keys_bdim, values_bdim)
    new_dimension = dimension + (values_bdim <= dimension)
    return sort_key_val(keys_trans, values, new_dimension), (values_bdim, values_bdim)
  elif keys_bdim is None:
    broadcast_dimensions = onp.delete(onp.arange(values.ndim), values_bdim)
    new_keys = broadcast_in_dim(keys, values.shape, broadcast_dimensions)
    new_dimension = dimension + (values_bdim <= dimension)
    return sort_key_val(new_keys, values, new_dimension), (values_bdim, values_bdim)
  elif values_bdim is None:
    broadcast_dimensions = onp.delete(onp.arange(keys.ndim), keys_bdim)
    new_values = broadcast_in_dim(values, keys.shape, broadcast_dimensions)
    new_dimension = dimension + (keys_bdim <= dimension)
    return sort_key_val(keys, new_values, new_dimension), (keys_bdim, keys_bdim)
  else:
    raise Exception  # unreachable

sort_key_val_p = Primitive('sort_key_val')
sort_key_val_p.multiple_results = True
sort_key_val_p.def_impl(partial(xla.apply_primitive, sort_key_val_p))
sort_key_val_p.def_abstract_eval(_sort_key_val_abstract_eval)
xla.translations[sort_key_val_p] = partial(standard_translate, 'sort_key_val')
ad.primitive_jvps[sort_key_val_p] = _sort_key_val_jvp
ad.primitive_transposes[sort_key_val_p] = _sort_key_val_transpose_rule
batching.primitive_batchers[sort_key_val_p] = _sort_key_val_batch_rule


def _tie_in_transpose_rule(t):
  return [ad_util.zero, t]

def _tie_in_batch_rule(batched_args, batch_dims):
  y = tie_in(*batched_args)
  _, bdim_y = batch_dims
  return y, bdim_y

tie_in_p = Primitive('tie_in')
tie_in_p.def_impl(lambda x, y: y)
tie_in_p.def_abstract_eval(lambda x, y: y)
xla.translations[tie_in_p] = lambda c, x, y: y
ad.deflinear(tie_in_p, _tie_in_transpose_rule)
batching.primitive_batchers[tie_in_p] = _tie_in_batch_rule
masking.shape_rules[tie_in_p] = lambda shape_exprs: shape_exprs[1]
masking.masking_rules[tie_in_p] = lambda vals, logical_shapes: vals[1]

shaped_identity_p = Primitive('shape_id')
shaped_identity_p.def_impl(lambda x, shape: x)
shaped_identity_p.def_abstract_eval(lambda x, shape: x)
xla.translations[shaped_identity_p] = lambda c, x, shape: x
ad.deflinear(shaped_identity_p, lambda t, shape: [shaped_identity(t)])
batching.primitive_batchers[shaped_identity_p] = \
    lambda a, d, shape: (shaped_identity(a[0]), d[0])


### constants


class _FilledConstant(xla.DeviceConstant):
  __slots__ = ["fill_value"]

  def __init__(self, fill_value, shape):
    assert type(fill_value) is onp.ndarray
    self.aval = ShapedArray(shape, _dtype(fill_value))
    self._npy_value = None

    self.fill_value = fill_value

  @property
  def _value(self):
    return onp.full(self.shape, self.fill_value)

  @staticmethod
  def constant_handler(c, filled_const, canonicalize_types=True):
    return c.Broadcast(
      c.NumpyArrayConstant(filled_const.fill_value, canonicalize_types),
      filled_const.shape)


class _IotaConstant(xla.DeviceConstant):
  __slots__ = ["axis"]

  def __init__(self, dtype, shape, axis):
    self.aval = ShapedArray(shape, onp.dtype(dtype))
    self._npy_value = None

    self.axis = axis

  @property
  def _value(self):
    if self._npy_value is None:
      iota = onp.arange(self.shape[self.axis], dtype=self.dtype)
      iota = iota.reshape([self.shape[self.axis] if i == self.axis else 1
                           for i in range(self.ndim)])
      self._npy_value = onp.broadcast_to(iota, self.shape)
    return self._npy_value

  @staticmethod
  def constant_handler(c, iota_constant, canonicalize_types=True):
    dtype = iota_constant.dtype
    if canonicalize_types:
      dtype = xla_bridge.canonicalize_dtype(dtype)
    return c.BroadcastedIota(dtype, iota_constant.shape, iota_constant.axis)


class _EyeConstant(xla.DeviceConstant):
  __slots__ = ["axes"]

  def __init__(self, shape, axes, dtype):
    self.aval = ShapedArray(shape, onp.dtype(dtype))
    self._npy_value = None

    self.axes = axes

  @property
  def _value(self):
    if self._npy_value is None:
      ones = [1] * self.ndim
      iotas = [onp.arange(self.shape[axis]).reshape(subvals(ones, [(axis, -1)]))
               for axis in self.axes]
      eyes = [i1 == i2 for i1, i2 in zip(iotas[:-1], iotas[1:])]
      result = onp.asarray(_reduce(operator.and_, eyes), self.dtype)
      self._npy_value = onp.broadcast_to(result, self.shape)
    return self._npy_value

  @staticmethod
  def constant_handler(c, diag_const, canonicalize_types=True):
    if canonicalize_types:
      etype = xla_bridge.dtype_to_etype(diag_const.dtype)
    else:
      etype = xla_client.dtype_to_etype(diag_const.dtype)
    etype = xla_bridge.dtype_to_etype(diag_const.dtype)
    iotas = [c.BroadcastedIota(onp.uint32, diag_const.shape, axis)
             for axis in diag_const.axes]
    eyes = [c.Eq(i1, i2) for i1, i2 in zip(iotas[:-1], iotas[1:])]
    return c.ConvertElementType(_reduce(c.And, eyes), etype)


for _t in [_FilledConstant, _IotaConstant, _EyeConstant]:
  xla_bridge.register_constant_handler(_t, _t.constant_handler)
  core.pytype_aval_mappings[_t] = ConcreteArray
  xla.pytype_aval_mappings[_t] = make_shaped_array
  xla.device_put_handlers[_t] = xla._instantiate_device_constant
  pxla.shard_arg_handlers[_t] = pxla._shard_array
  xla.canonicalize_dtype_handlers[_t] = _identity
  ad_util.jaxval_adders[_t] = add
  ad_util.jaxval_zeros_likers[_t] = zeros_like_array


### stop-gradient

def _stop_gradient_jvp_rule(primals, tangents):
  # if we don't call stop_gradient here, we'd only peel off one autodiff tracer
  x, = primals
  return stop_gradient(x), ad_util.zero

def _stop_gradient_batch_rule(batched_args, batch_dims):
  x, = batched_args
  dim, = batch_dims
  return stop_gradient(x), dim

stop_gradient_p = Primitive('stop_gradient')
stop_gradient_p.def_impl(_identity)
stop_gradient_p.def_abstract_eval(_identity)
xla.translations[stop_gradient_p] = lambda c, x: x
ad.primitive_jvps[stop_gradient_p] = _stop_gradient_jvp_rule
batching.primitive_batchers[stop_gradient_p] = _stop_gradient_batch_rule


### util

_ndim = onp.ndim


def _dilate_shape(shape, dilation):
  """Utility function for computing the shape resulting from a dilation."""
  if not onp.all(onp.greater(dilation, 0)):
    msg = "All dilations must be positive, got {}."
    raise TypeError(msg.format(dilation))
  dilation = (1,) * (len(shape) - len(dilation)) + tuple(dilation)
  return onp.multiply(dilation, onp.subtract(shape, 1)) + 1



def padtype_to_pads(in_shape, window_shape, window_strides, padding):
  """Convert padding string to list of pairs of pad values."""
  PaddingType = xla_client.PaddingType

  if isinstance(padding, str):
    mapping = {'VALID': PaddingType.VALID, 'SAME': PaddingType.SAME}
    try:
      padding = mapping[padding.upper()]
    except KeyError:
      msg = "Unrecognized padding type: expected 'VALID' or 'SAME', got {}."
      raise RuntimeError(msg.format(padding))

  if padding == PaddingType.SAME:
    out_shape = onp.ceil(onp.true_divide(in_shape, window_strides)).astype(int)
    pad_sizes = [_max((out_size - 1) * stride + window_shape - in_size, 0)
                 for out_size, stride, window_shape, in_size
                 in zip(out_shape, window_strides, window_shape, in_shape)]
    return [(pad_size // 2, pad_size - pad_size // 2) for pad_size in pad_sizes]
  elif padding == PaddingType.VALID:
    return [(0, 0)] * len(in_shape)
  else:
    msg = "Unknown padding type: {}."
    raise TypeError(msg.format(padding))


def _check_same_dtypes(name, ignore_fp_precision, *dtypes):
  """Check that dtypes agree, possibly ignoring float precision."""
  # the `ignore_fp_precision` flag exists because the XLA shape inference logic
  # allows mixed floating point precision, but the HLO verifier often rejects it
  dtypes = list(map(onp.dtype, dtypes))  # canonicalize
  if ignore_fp_precision:
    dtypes = [
        onp.floating if onp.issubdtype(dtype, onp.floating)
        else onp.complexfloating if onp.issubdtype(dtype, onp.complexfloating)
        else dtype for dtype in dtypes]
  if len({xla_bridge.canonicalize_dtype(t) for t in dtypes}) != 1:
    if ignore_fp_precision:
      msg = ("{} requires arguments to have same dtypes up to floating point "
             "precision, got {}.")
    else:
      msg = "{} requires arguments to have the same dtypes, got {}."
    raise TypeError(msg.format(name, ", ".join(map(str, dtypes))))


def _check_conv_shapes(name, lhs_shape, rhs_shape, window_strides):
  """Check that conv shapes are valid and are consistent with window_strides."""
  if len(lhs_shape) != len(rhs_shape):
    msg = "Arguments to {} must have same rank, got {} and {}."
    raise TypeError(msg.format(name, len(lhs_shape), len(rhs_shape)))
  if len(lhs_shape) < 2:
    msg = "Arguments to {} must have rank at least 2, got {} and {}."
    raise TypeError(msg.format(name, len(lhs_shape), len(rhs_shape)))
  if lhs_shape[1] != rhs_shape[1]:
    msg = "Arguments to {} must agree on input feature size, got {} and {}."
    raise TypeError(msg.format(name, lhs_shape[1], rhs_shape[1]))
  _check_shapelike(name, "window_strides", window_strides)
  if not onp.all(onp.greater(window_strides, 0)):
    msg = "All elements of window_strides must be positive, got {}."
    raise TypeError(msg.format(window_strides))
  if len(window_strides) != len(lhs_shape) - 2:
    msg = "{} window_strides has wrong length: expected {}, got {}."
    expected_length = len(lhs_shape) - 2
    raise TypeError(msg.format(name, expected_length, len(window_strides)))


def conv_shape_tuple(lhs_shape, rhs_shape, strides, pads):
  """Compute the shape tuple of a conv given input shapes in canonical order."""
  if isinstance(pads, str):
    pads = padtype_to_pads(lhs_shape[2:], rhs_shape[2:], strides, pads)
  if len(pads) != len(lhs_shape) - 2:
    msg = "Wrong number of explicit pads for convolution: expected {}, got {}."
    raise TypeError(msg.format(len(lhs_shape) - 2, len(pads)))

  lhs_padded = onp.add(lhs_shape[2:], onp.add(*zip(*pads)))
  out_space = onp.floor_divide(
      onp.subtract(lhs_padded, rhs_shape[2:]), strides) + 1
  out_space = onp.maximum(0, out_space)
  out_shape = (lhs_shape[0], rhs_shape[0]) + tuple(out_space)
  return tuple(out_shape)


def conv_general_shape_tuple(lhs_shape, rhs_shape, window_strides, padding,
                             dimension_numbers):
  lhs_perm, rhs_perm, out_perm = conv_general_permutations(dimension_numbers)
  lhs_trans = onp.take(lhs_shape, lhs_perm)
  rhs_trans = onp.take(rhs_shape, rhs_perm)
  out_trans = conv_shape_tuple(lhs_trans, rhs_trans, window_strides, padding)
  return tuple(onp.take(out_trans, onp.argsort(out_perm)))


def conv_transpose_shape_tuple(lhs_shape, rhs_shape, window_strides, padding,
                             dimension_numbers):
  lhs_perm, rhs_perm, out_perm = conv_general_permutations(dimension_numbers)
  lhs_trans = onp.take(lhs_shape, lhs_perm)
  rhs_trans = onp.take(rhs_shape, rhs_perm)
  if isinstance(padding, str):
    padding = [_conv_transpose_padding(k, s, padding)
               for k,s in zip(rhs_trans[2:], window_strides)]
  padding = list(map(onp.sum, padding))
  unpad_out_space = [(i-1) * s - k + 2
                     for i, k, s in zip(lhs_trans[2:],
                                        rhs_trans[2:],
                                        window_strides)]
  out_space = onp.sum([unpad_out_space, padding], axis=0).tolist()
  out_trans = tuple((lhs_trans[0], rhs_trans[0]) + tuple(out_space))
  return tuple(onp.take(out_trans, onp.argsort(out_perm)))


def _check_shapelike(fun_name, arg_name, obj):
  """Check that `obj` is a shape-like value (e.g. tuple of nonnegative ints)."""
  if (type(obj) is masking.ShapeExpr
      or type(obj) is tuple and any(type(d) is masking.Poly for d in obj)):
    return obj
  if not isinstance(obj, (tuple, list, onp.ndarray)):
    msg = "{} {} must be of type tuple/list/ndarray, got {}."
    raise TypeError(msg.format(fun_name, arg_name, type(obj)))
  # bool(obj) for an ndarray raises an error, so we check len
  if not len(obj):  # pylint: disable=g-explicit-length-test
    return
  obj_arr = onp.array(obj)
  if obj_arr.ndim != 1:
    msg = "{} {} must be rank 1, got {}."
    raise TypeError(msg.format(obj_arr.ndim))
  if not onp.issubdtype(obj_arr.dtype, onp.integer):
    msg = "{} {} must have every element be an integer type, got {}."
    raise TypeError(msg.format(fun_name, arg_name, tuple(map(type, obj))))
  if not (obj_arr >= 0).all():
    msg = "{} {} must have every element be nonnegative, got {}."
    raise TypeError(msg.format(fun_name, arg_name, obj))


def _dynamic_slice_indices(operand, start_indices):
  if not isinstance(start_indices, (tuple, list)):
    if start_indices.ndim != 1:
      raise ValueError("Slice indices must be a 1D sequence, got {}"
                       .format(start_indices.shape))
    start_indices = [reshape(slice(start_indices, [i], [i+1]), ())
                     for i in range(operand.ndim)]
  else:
    start_indices = [onp.asarray(i) if isinstance(i, int) else i
                     for i in start_indices]
  if len(start_indices) != operand.ndim:
    msg = ("Length of slice indices must match number of operand dimensions ({} "
          "vs {})")
    raise ValueError(msg.format(len(start_indices, operand.shape)))
  # map int over operand.shape to raise any dynamic-shape errors
  return [select(lt(i, _const(i, 0)), add(i, _const(i, int(d))), i)
          for i, d in zip(start_indices, operand.shape)]



def _const(example, val):
  return onp.array(val, _dtype(example))

_zeros = partial(full_like, fill_value=0)
_zero = partial(full_like, shape=(), fill_value=0)
_ones = partial(full_like, fill_value=1)
_one = partial(full_like, shape=(), fill_value=1)
_twos = partial(full_like, fill_value=2)
_two = partial(full_like, shape=(), fill_value=2)

_dtype = dtype = onp.result_type
_iscomplex = lambda x: onp.issubdtype(_dtype(x), onp.complexfloating)


def ranges_like(*xs):
  start = 0
  for x in xs:
    x_len = len(x)
    yield range(start, start + x_len)
    start += x_len


def remaining(original, *removed_lists):
  blacklist = set(itertools.chain(*removed_lists))
  return [i for i in original if i not in blacklist]


def _canonicalize_precision(precision):
  if precision is None:
    return None
  if isinstance(precision, Precision):
    return precision
  else:
    msg = "Precision argument must be None or a lax.Precision value; got {}"
    raise ValueError(msg.format(precision))


# lhs_spec and out_spec are lists containing
#   [batch dim, feature dim, spatial dims ...]
# rhs_spec is a list containing:
#   [out feature dim, in feature dim, spatial dims ...]
class ConvDimensionNumbers(collections.namedtuple(
    "ConvDimensionNumbers", ["lhs_spec", "rhs_spec", "out_spec"])):
  """Describes batch, spatial, and feature dimensions of a convolution.

  Args:
    lhs_spec: a tuple of nonnegative integer dimension numbers containing
      `(batch dimension, feature dimension, spatial dimensions...)`.
    rhs_spec: a tuple of nonnegative integer dimension numbers containing
      `(out feature dimension, in feature dimension, spatial dimensions...)`.
    out_spec: a tuple of nonnegative integer dimension numbers containing
      `(batch dimension, feature dimension, spatial dimensions...)`.
  """

def conv_dimension_numbers(lhs_shape, rhs_shape, dimension_numbers):
  """Converts convolution `dimension_numbers` to a `ConvDimensionNumbers`.

  Args:
    lhs_shape: tuple of nonnegative integers, shape of the convolution input.
    rhs_shape: tuple of nonnegative integers, shape of the convolution kernel.
    dimension_numbers: None or a tuple/list of strings, following the
      convolution dimension number specification format in xla_client.py.

  Returns:
    A `ConvDimensionNumbers` object that represents `dimension_numbers` in the
    canonical form used by lax functions.
  """
  if len(lhs_shape) != len(rhs_shape):
    msg = "convolution requires lhs and rhs ndim to be equal, got {} and {}."
    raise TypeError(msg.format(len(lhs_shape), len(rhs_shape)))

  if dimension_numbers is None:
    iota = tuple(range(len(lhs_shape)))
    return ConvDimensionNumbers(iota, iota, iota)
  elif isinstance(dimension_numbers, (list, tuple)):
    if len(dimension_numbers) != 3:
      msg = "convolution dimension_numbers list/tuple must be length 3, got {}."
      raise TypeError(msg.format(len(dimension_numbers)))
    if not all(isinstance(elt, str) for elt in dimension_numbers):
      msg = "convolution dimension_numbers elements must be strings, got {}."
      raise TypeError(msg.format(tuple(map(type, dimension_numbers))))
    msg = ("convolution dimension_numbers[{}] must have len equal to the ndim "
           "of lhs and rhs, got {} for lhs and rhs shapes {} and {}.")
    for i, elt in enumerate(dimension_numbers):
      if len(elt) != len(lhs_shape):
        raise TypeError(msg.format(i, len(elt), lhs_shape, rhs_shape))

    lhs_spec, rhs_spec, out_spec = conv_general_permutations(dimension_numbers)
    return ConvDimensionNumbers(lhs_spec, rhs_spec, out_spec)
  else:
    msg = "convolution dimension_numbers must be tuple/list or None, got {}."
    raise TypeError(msg.format(type(dimension_numbers)))


def conv_general_permutations(dimension_numbers):
  """Utility for convolution dimension permutations relative to Conv HLO."""
  lhs_spec, rhs_spec, out_spec = dimension_numbers
  lhs_char, rhs_char, out_char = charpairs = ("N", "C"), ("O", "I"), ("N", "C")
  for i, (a, b) in enumerate(charpairs):
    if not dimension_numbers[i].count(a) == dimension_numbers[i].count(b) == 1:
      msg = ("convolution dimension_numbers[{}] must contain the characters "
             "'{}' and '{}' exatly once, got {}.")
      raise TypeError(msg.format(i, a, b, dimension_numbers[i]))
    if len(dimension_numbers[i]) != len(set(dimension_numbers[i])):
      msg = ("convolution dimension_numbers[{}] cannot have duplicate "
             "characters, got {}.")
      raise TypeError(msg.format(i, dimension_numbers[i]))
  if not (set(lhs_spec) - set(lhs_char) == set(rhs_spec) - set(rhs_char) ==
          set(out_spec) - set(out_char)):
    msg = ("convolution dimension_numbers elements must each have the same "
           "set of spatial characters, got {}.")
    raise TypeError(msg.format(dimension_numbers))

  def getperm(spec, charpair):
    spatial = (i for i, c in enumerate(spec) if c not in charpair)
    if spec is not rhs_spec:
      spatial = sorted(spatial, key=lambda i: rhs_spec.index(spec[i]))
    return (spec.index(charpair[0]), spec.index(charpair[1])) + tuple(spatial)

  lhs_perm, rhs_perm, out_perm = map(getperm, dimension_numbers, charpairs)
  return lhs_perm, rhs_perm, out_perm


def _conv_general_proto(dimension_numbers):
  assert type(dimension_numbers) is ConvDimensionNumbers
  lhs_spec, rhs_spec, out_spec = dimension_numbers
  proto = xla_client.ConvolutionDimensionNumbers()
  proto.input_batch_dimension = lhs_spec[0]
  proto.input_feature_dimension = lhs_spec[1]
  proto.output_batch_dimension = out_spec[0]
  proto.output_feature_dimension = out_spec[1]
  proto.kernel_output_feature_dimension = rhs_spec[0]
  proto.kernel_input_feature_dimension = rhs_spec[1]
  proto.input_spatial_dimensions.extend(lhs_spec[2:])
  proto.kernel_spatial_dimensions.extend(rhs_spec[2:])
  proto.output_spatial_dimensions.extend(out_spec[2:])
  return proto


def _conv_general_vjp_lhs_padding(
    in_shape, window_dimensions, window_strides, out_shape, padding,
    lhs_dilation, rhs_dilation):
  lhs_dilated_shape = _dilate_shape(in_shape, lhs_dilation)
  rhs_dilated_shape = _dilate_shape(window_dimensions, rhs_dilation)
  out_dilated_shape = _dilate_shape(out_shape, window_strides)
  pad_before = onp.subtract(rhs_dilated_shape, [lo for lo, _ in padding]) - 1
  pad_after = (onp.add(lhs_dilated_shape, rhs_dilated_shape) - 1
               - out_dilated_shape - pad_before)
  return zip(pad_before, pad_after)


def _conv_general_vjp_rhs_padding(
    in_shape, window_dimensions, window_strides, out_shape, padding,
    lhs_dilation, rhs_dilation):
  lhs_dilated_shape = _dilate_shape(in_shape, lhs_dilation)
  rhs_dilated_shape = _dilate_shape(window_dimensions, rhs_dilation)
  out_dilated_shape = _dilate_shape(out_shape, window_strides)
  total_in_pad = out_dilated_shape + rhs_dilated_shape - lhs_dilated_shape - 1
  return [(pad[0], tot - pad[0]) for pad, tot in zip(padding, total_in_pad)]


def _balanced_eq(x, z, y):
  return div(select(_eq_meet(x, z), _ones(z), _zeros(z)),
             select(_eq_meet(y, z), _twos(z), _ones(z)))


def _eq_meet(a, b):
  a_dtype, b_dtype = _dtype(a), _dtype(b)
  if a_dtype != b_dtype:
    higher_dtype = onp.promote_types(a_dtype, b_dtype)
    if higher_dtype == a_dtype:
      a = convert_element_type(a, b_dtype)
    else:
      b = convert_element_type(b, a_dtype)
  return eq(a, b)


def subvals(lst, replace):
  lst = list(lst)
  for i, v in replace:
    lst[i] = v
  return tuple(lst)


def _abstractify(x):
  return raise_to_shaped(core.get_aval(x))


def _check_user_dtype_supported(dtype, fun_name=None):
  if dtype is not None and onp.dtype(dtype) != xla_bridge.canonicalize_dtype(dtype):
    msg = ("Explicitly requested dtype {} {} is not available, "
           "and will be truncated to dtype {}. To enable more dtypes, set the "
           "jax_enable_x64 configuration option or the JAX_ENABLE_X64 shell "
           "environment variable. "
           "See https://github.com/google/jax#current-gotchas for more.")
    fun_name = "requested in {}".format(fun_name) if fun_name else ""
    truncated_dtype = xla_bridge.canonicalize_dtype(dtype).name
    warnings.warn(msg.format(dtype, fun_name , truncated_dtype))
