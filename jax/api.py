# coding=utf-8
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
JAX user-facing transformations and utilities.

The transformations here mostly wrap internal transformations, providing
convenience flags to control behavior and handling Python containers of
arguments and outputs. The Python containers handled are pytrees (see
tree_util.py), which include nested tuples/lists/dicts, where the leaves are
arrays.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import itertools as it
import operator as op
import os
import threading
from warnings import warn

import numpy as onp
from contextlib import contextmanager
from distutils.util import strtobool
import six
from six.moves import reduce

from . import core
from . import linear_util as lu
from . import ad_util
from .core import eval_jaxpr
from .api_util import (wraps, flatten_fun, apply_flat_fun, flatten_fun_nokwargs,
                       flatten_fun_nokwargs2, apply_flat_fun_nokwargs)
from .tree_util import (tree_map, tree_flatten, tree_unflatten, tree_structure,
                        tree_transpose, tree_leaves, tree_multimap)
from .util import (unzip2, unzip3, curry, partial, safe_map, safe_zip,
                   WrapHashably, Hashable, prod, split_list)
from .lib.xla_bridge import (canonicalize_dtype, device_count,
                             local_device_count, devices, host_id)
from .abstract_arrays import ShapedArray, raise_to_shaped
from .interpreters import partial_eval as pe
from .interpreters import xla
from .interpreters import pxla
from .interpreters import ad
from .interpreters import batching
from .interpreters import parallel
from .interpreters import masking
from .interpreters.masking import shapecheck
from .config import flags, config

map = safe_map
zip = safe_zip

FLAGS = flags.FLAGS
flags.DEFINE_bool("jax_disable_jit",
                  strtobool(os.getenv("JAX_DISABLE_JIT", "False")),
                  "Disable JIT compilation and just call original Python.")


def _check_callable(fun):
  if not callable(fun):
    raise TypeError("Expected a callable value, got {}".format(fun))

class _ThreadLocalState(threading.local):
  def __init__(self):
    self.jit_is_disabled = False

_thread_local_state = _ThreadLocalState()

def jit(fun, static_argnums=(), device=None, backend=None):
  """Sets up `fun` for just-in-time compilation with XLA.

  Args:
    fun: Function to be jitted. Should be a pure function, as side-effects may
      only be executed once. Its arguments and return value should be arrays,
      scalars, or (nested) standard Python containers (tuple/list/dict) thereof.

      Positional arguments indicated by `static_argnums` can be anything at all,
      provided they are hashable and have an equality operation defined. Static
      arguments are included as part of a compilation cache key, which is why
      hash and equality operators must be defined.
    static_argnums: A tuple of ints specifying which positional arguments to
      treat as static (compile-time constant). Operations that only depend on
      static arguments will be constant-folded. Calling the jitted function with
      different values for these constants will trigger recompilation. If the
      jitted function is called with fewer positional arguments than indicated
      by `static_argnums` then an error is raised. Defaults to ().
    device: This is an experimental feature and the API is likely to change.
      Optional, the Device the jitted function will run on. (Available devices
      can be retrieved via ``jax.devices()``.) The default is inherited from
      XLA's DeviceAssignment logic and is usually to use ``jax.devices()[0]``.
    backend: This is an experimental feature and the API is likely to change.
      Optional, a string representing the xla backend. 'cpu','gpu', or 'tpu'.

  Returns:
    A wrapped version of `fun`, set up for just-in-time compilation.

  In the following example, `selu` can be compiled into a single fused kernel by
  XLA:

  >>> @jax.jit
  >>> def selu(x, alpha=1.67, lmbda=1.05):
  >>>   return lmbda * jax.numpy.where(x > 0, x, alpha * jax.numpy.exp(x) - alpha)
  >>>
  >>> key = jax.random.PRNGKey(0)
  >>> x = jax.random.normal(key, (10,))
  >>> print(selu(x))
  [-0.54485154  0.27744263 -0.29255125 -0.91421586 -0.62452525 -0.2474813
   -0.8574326  -0.7823267   0.7682731   0.59566754]
  """
  _check_callable(fun)
  if isinstance(static_argnums, int):
    static_argnums = (static_argnums,)

  @wraps(fun)
  def f_jitted(*args, **kwargs):
    if _thread_local_state.jit_is_disabled or config.read('jax_disable_jit'):
      return fun(*args, **kwargs)
    if static_argnums and max(static_argnums) >= len(args):
      msg = ("Jitted function has static_argnums={} but was called with only {}"
             " positional arguments.")
      raise TypeError(msg.format(static_argnums, len(args)))
    f = lu.wrap_init(fun)
    if static_argnums:
      dyn_argnums = [i for i in range(len(args)) if i not in static_argnums]
      f, dyn_args = _argnums_partial(f, dyn_argnums, args)
    else:
      dyn_args = args
    args_flat, in_tree = tree_flatten((dyn_args, kwargs))
    _check_args(args_flat)
    flat_fun, out_tree = flatten_fun(f, in_tree)
    out = xla.xla_call(flat_fun, *args_flat, device=device, backend=backend)
    return tree_unflatten(out_tree(), out)

  jitted_name =  "jit({}, static_argnums={})"
  f_jitted.__name__ = jitted_name.format(f_jitted.__name__, static_argnums)
  return f_jitted

@contextmanager
def disable_jit():
  """Context manager that disables `jit` behavior under its dynamic context.

  For debugging purposes, it is useful to have a mechanism that disables `jit`
  everywhere in a dynamic context.

  Values that have a data dependence on the arguments to a jitted function are
  traced and abstracted. For example, an abstract value may be a ShapedArray
  instance, representing the set of all possible arrays with a given shape and
  dtype, but not representing one concrete array with specific values. You might
  notice those if you use a benign side-effecting operation in a jitted
  function, like a print:

  >>> @jax.jit
  >>> def f(x):
  ...   y = x * 2
  ...   print("Value of y is", y)
  ...   return y + 3
  ...
  >>> print(f(jax.numpy.array([1, 2, 3])))
  Value of y is Traced<ShapedArray(int32[3]):JaxprTrace(level=-1/1)>
  [5 7 9]

  Here `y` has been abstracted by `jit` to a `ShapedArray`, which represents an
  array with a fixed shape and type but an arbitrary value. It's also traced. If
  we want to see a concrete value while debugging, and avoid the tracer too, we
  can use the `disable_jit` context manager:

  >>> with jax.disable_jit():
  >>>   print(f(np.array([1, 2, 3])))
  >>>
  Value of y is [2 4 6]
  [5 7 9]
  """
  try:
    prev_val = _thread_local_state.jit_is_disabled
    _thread_local_state.jit_is_disabled = True
    yield
  finally:
    _thread_local_state.jit_is_disabled = prev_val


def xla_computation(fun, static_argnums=(), axis_env=None, backend=None):
  """Creates a function that produces its XLA computation given example args.

  Args:
    fun: Function from which to form XLA computations.
    static_argnums: See the ``jax.jit`` docstring.
    axis_env: Optional, a list of pairs where the first element is an axis name
      and the second element is a positive integer representing the size of the
      mapped axis with that name. This parameter is useful when lowering
      functions that involve parallel communication collectives, and it
      specifies the axis name/size environment that would be set up by
      applications of ``jax.pmap``. See the examples below.
    backend: This is an experimental feature and the API is likely to change.
      Optional, a string representing the xla backend. 'cpu','gpu', or 'tpu'.

  Returns:
    A wrapped version of ``fun`` that when applied to example arguments returns a
    built XLA Computation (see xla_client.py), from which representations of the
    unoptimized XLA HLO computation can be extracted using methods like
    ``GetHloText``, ``GetSerializedProto``, and ``GetHloDotGraph``.

  For example:

  >>> def f(x): return jax.numpy.sin(jax.numpy.cos(x))
  >>> c = jax.xla_computation(f)(3.)
  >>> print(c.GetHloText())
  HloModule jaxpr_computation__4.5
  ENTRY jaxpr_computation__4.5 {
    tuple.1 = () tuple()
    parameter.2 = f32[] parameter(0)
    cosine.3 = f32[] cosine(parameter.2)
    ROOT sine.4 = f32[] sine(cosine.3)
  }

  Here's an example that involves a parallel collective and axis name:

  >>> def f(x): return x - jax.lax.psum(x, 'i')
  >>> c = jax.xla_computation(f, axis_env=[('i', 4)])(2)
  >>> print(c.GetHloText())
  HloModule jaxpr_computation.9
  primitive_computation.3 {
    parameter.4 = s32[] parameter(0)
    parameter.5 = s32[] parameter(1)
    ROOT add.6 = s32[] add(parameter.4, parameter.5)
  }
  ENTRY jaxpr_computation.9 {
    tuple.1 = () tuple()
    parameter.2 = s32[] parameter(0)
    all-reduce.7 = s32[] all-reduce(parameter.2), replica_groups={{0,1,2,3}}, to_apply=primitive_computation.3
    ROOT subtract.8 = s32[] subtract(parameter.2, all-reduce.7)
  }

  Notice the ``replica_groups`` that were generated. Here's an example that
  generates more interesting ``replica_groups``:

  >>> def g(x):
  ...   rowsum = lax.psum(x, 'i')
  ...   colsum = lax.psum(x, 'j')
  ...   allsum = lax.psum(x, ('i', 'j'))
  ...   return rowsum, colsum, allsum
  ...
  >>> axis_env = [('i', 4), ('j', 2)]
  >>> c = xla_computation(g, axis_env=axis_env)(5.)
  >>> print(c.GetHloText())
  HloModule jaxpr_computation__1.19
  [removed uninteresting text here]
  ENTRY jaxpr_computation__1.19 {
    tuple.1 = () tuple()
    parameter.2 = f32[] parameter(0)
    all-reduce.7 = f32[] all-reduce(parameter.2), replica_groups={{0,2,4,6},{1,3,5,7}}, to_apply=primitive_computation__1.3
    all-reduce.12 = f32[] all-reduce(parameter.2), replica_groups={{0,1},{2,3},{4,5},{6,7}}, to_apply=primitive_computation__1.8
    all-reduce.17 = f32[] all-reduce(parameter.2), replica_groups={{0,1,2,3,4,5,6,7}}, to_apply=primitive_computation__1.13
    ROOT tuple.18 = (f32[], f32[], f32[]) tuple(all-reduce.7, all-reduce.12, all-reduce.17)
  }
  """
  _check_callable(fun)

  def pv_like(x):
    aval = xla.abstractify(x)
    return pe.PartialVal((aval, core.unit))

  def make_axis_env(nreps):
    if axis_env is None:
      return xla.AxisEnv(nreps)
    else:
      nreps = nreps * prod(size for name, size in axis_env)
      names, sizes = zip(*axis_env)
      return xla.AxisEnv(nreps, names, sizes)

  @wraps(fun)
  def computation_maker(*args, **kwargs):
    wrapped = lu.wrap_init(fun)
    jax_args, in_tree = tree_flatten((args, kwargs))
    jaxtree_fun, out_tree = flatten_fun(wrapped, in_tree)
    pvals = map(pv_like, jax_args)
    jaxpr, _, consts = pe.trace_to_jaxpr(jaxtree_fun, pvals)
    axis_env_ = make_axis_env(xla.jaxpr_replicas(jaxpr))
    return xla.build_jaxpr(jaxpr, backend, axis_env_, consts,
                           *map(xla.abstractify, jax_args))
  return computation_maker

def grad(fun, argnums=0, has_aux=False, holomorphic=False):
  """Creates a function which evaluates the gradient of `fun`.

  Args:
    fun: Function to be differentiated. Its arguments at positions specified by
      `argnums` should be arrays, scalars, or standard Python containers. It
      should return a scalar (which includes arrays with shape `()` but not
      arrays with shape `(1,)` etc.)
    argnums: Optional, integer or tuple of integers. Specifies which positional
      argument(s) to differentiate with respect to (default 0).
    has_aux: Optional, bool. Indicates whether `fun` returns a pair where the
      first element is considered the output of the mathematical function to be
      differentiated and the second element is auxiliary data. Default False.
    holomorphic: Optional, bool. Indicates whether `fun` is promised to be
      holomorphic. Default False.

  Returns:
    A function with the same arguments as `fun`, that evaluates the gradient of
    `fun`. If `argnums` is an integer then the gradient has the same shape and
    type as the positional argument indicated by that integer. If argnums is a
    tuple of integers, the gradient is a tuple of values with the same shapes
    and types as the corresponding arguments. If `has_aux` is True then a pair
    of (gradient, auxiliary_data) is returned.

  For example:

  >>> grad_tanh = jax.grad(jax.numpy.tanh)
  >>> print(grad_tanh(0.2))
  0.961043
  """
  value_and_grad_f = value_and_grad(fun, argnums, has_aux=has_aux,
                                    holomorphic=holomorphic)

  docstr = ("Gradient of {fun} with respect to positional argument(s) "
            "{argnums}. Takes the same arguments as {fun} but returns the "
            "gradient, which has the same shape as the arguments at "
            "positions {argnums}.")

  @wraps(fun, docstr=docstr, argnums=argnums)
  def grad_f(*args, **kwargs):
    _, g = value_and_grad_f(*args, **kwargs)
    return g

  @wraps(fun, docstr=docstr, argnums=argnums)
  def grad_f_aux(*args, **kwargs):
    (_, aux), g = value_and_grad_f(*args, **kwargs)
    return g, aux

  return grad_f_aux if has_aux else grad_f

def value_and_grad(fun, argnums=0, has_aux=False, holomorphic=False):
  """Creates a function which evaluates both `fun` and the gradient of `fun`.

  Args:
    fun: Function to be differentiated. Its arguments at positions specified by
      `argnums` should be arrays, scalars, or standard Python containers. It
      should return a scalar (which includes arrays with shape `()` but not
      arrays with shape `(1,)` etc.)
    argnums: Optional, integer or tuple of integers. Specifies which positional
      argument(s) to differentiate with respect to (default 0).
    has_aux: Optional, bool. Indicates whether `fun` returns a pair where the
     first element is considered the output of the mathematical function to be
     differentiated and the second element is auxiliary data. Default False.
    holomorphic: Optional, bool. Indicates whether `fun` is promised to be
      holomorphic. Default False.

  Returns:
    A function with the same arguments as `fun` that evaluates both `fun` and
    the gradient of `fun` and returns them as a pair (a two-element tuple). If
    `argnums` is an integer then the gradient has the same shape and type as the
    positional argument indicated by that integer. If argnums is a tuple of
    integers, the gradient is a tuple of values with the same shapes and types
    as the corresponding arguments.
  """

  docstr = ("Value and gradient of {fun} with respect to positional "
            "argument(s) {argnums}. Takes the same arguments as {fun} but "
            "returns a two-element tuple where the first element is the value "
            "of {fun} and the second element is the gradient, which has the "
            "same shape as the arguments at positions {argnums}.")

  _check_callable(fun)

  @wraps(fun, docstr=docstr, argnums=argnums)
  def value_and_grad_f(*args, **kwargs):
    f = lu.wrap_init(fun, kwargs)
    f_partial, dyn_args = _argnums_partial(f, argnums, args)
    if not has_aux:
      ans, vjp_py = vjp(f_partial, *dyn_args)
    else:
      ans, vjp_py, aux = vjp(f_partial, *dyn_args, has_aux=True)
    _check_scalar(ans)
    dtype = onp.result_type(ans)
    if not (holomorphic or onp.issubdtype(dtype, onp.floating)):
      msg = ("Gradient only defined for real-output functions (with dtype that "
             "is a subdtype of np.floating), but got dtype {}. For holomorphic "
             "differentiation, pass holomorphic=True.")
      raise TypeError(msg.format(dtype))
    g = vjp_py(onp.ones((), dtype=dtype))
    g = g[0] if isinstance(argnums, int) else g
    if not has_aux:
      return ans, g
    else:
      return (ans, aux), g

  return value_and_grad_f

def _check_scalar(x):
  msg = "Gradient only defined for scalar-output functions. Output {}.".format
  try:
    aval = core.get_aval(x)
  except TypeError:
    raise TypeError(msg("was {}".format(x)))
  else:
    if isinstance(aval, ShapedArray):
      if aval.shape != ():
        raise TypeError(msg("had shape: {}".format(aval.shape)))
    else:
      raise TypeError(msg("had abstract value {}".format(aval)))


def jacfwd(fun, argnums=0, holomorphic=False):
  """Jacobian of `fun` evaluated column-by-column using forward-mode AD.

  Args:
    fun: Function whose Jacobian is to be computed.
    argnums: Optional, integer or tuple of integers. Specifies which positional
      argument(s) to differentiate with respect to (default `0`).
    holomorphic: Optional, bool. Indicates whether `fun` is promised to be
      holomorphic. Default False.

  Returns:
    A function with the same arguments as `fun`, that evaluates the Jacobian of
    `fun` using forward-mode automatic differentiation.

  >>> def f(x):
  ...   return jax.numpy.asarray(
  ...     [x[0], 5*x[2], 4*x[1]**2 - 2*x[2], x[2] * jax.numpy.sin(x[0])])
  ...
  >>> print(jax.jacfwd(f)(np.array([1., 2., 3.])))
  [[ 1.        ,  0.        ,  0.        ],
   [ 0.        ,  0.        ,  5.        ],
   [ 0.        , 16.        , -2.        ],
   [ 1.6209068 ,  0.        ,  0.84147096]]
  """

  def jacfun(*args, **kwargs):
    f = lu.wrap_init(fun, kwargs)
    f_partial, dyn_args = _argnums_partial(f, argnums, args)
    holomorphic or tree_map(_check_real_input_jacfwd, dyn_args)
    pushfwd = partial(jvp, f_partial, dyn_args)
    y, jac = vmap(pushfwd, out_axes=(None, batching.last))(_std_basis(dyn_args))
    example_args = dyn_args[0] if isinstance(argnums, int) else dyn_args
    return tree_map(partial(_unravel_array_into_pytree, example_args, -1), jac)

  return jacfun

def _check_real_input_jacfwd(x):
  aval = core.get_aval(x)
  if not onp.issubdtype(aval.dtype, onp.floating):
    msg = ("jacfwd only defined for functions with input dtypes that are "
           "sub-dtypes of `np.floating` (i.e. that model real values), but got "
           "{}. For holomorphic differentiation, pass holomorphic=True.")
    raise TypeError(msg.format(aval.dtype.name))


def jacrev(fun, argnums=0, holomorphic=False):
  """Jacobian of `fun` evaluated row-by-row using reverse-mode AD.

  Args:
    fun: Function whose Jacobian is to be computed.
    argnums: Optional, integer or tuple of integers. Specifies which positional
      argument(s) to differentiate with respect to (default `0`).
    holomorphic: Optional, bool. Indicates whether `fun` is promised to be
      holomorphic. Default False.

  Returns:
    A function with the same arguments as `fun`, that evaluates the Jacobian of
    `fun` using reverse-mode automatic differentiation.

  >>> def f(x):
  ...   return jax.numpy.asarray(
  ...     [x[0], 5*x[2], 4*x[1]**2 - 2*x[2], x[2] * jax.numpy.sin(x[0])])
  ...
  >>> print(jax.jacrev(f)(np.array([1., 2., 3.])))
  [[ 1.        ,  0.        ,  0.        ],
   [ 0.        ,  0.        ,  5.        ],
   [ 0.        , 16.        , -2.        ],
   [ 1.6209068 ,  0.        ,  0.84147096]]
  """
  def jacfun(*args, **kwargs):
    f = lu.wrap_init(fun, kwargs)
    f_partial, dyn_args = _argnums_partial(f, argnums, args)
    y, pullback = vjp(f_partial, *dyn_args)
    holomorphic or tree_map(_check_real_output_jacrev, y)
    jac = vmap(pullback)(_std_basis(y))
    jac = jac[0] if isinstance(argnums, int) else jac
    example_args = dyn_args[0] if isinstance(argnums, int) else dyn_args
    jac = tree_map(partial(_unravel_array_into_pytree, y, 0), jac)
    return tree_transpose(tree_structure(example_args), tree_structure(y), jac)

  return jacfun
jacobian = jacrev

def _check_real_output_jacrev(x):
  aval = core.get_aval(x)
  if not onp.issubdtype(aval.dtype, onp.floating):
    msg = ("jacrev only defined for functions with output dtypes that are "
           "sub-dtypes of `np.floating` (i.e. that model real values), but got "
           "{}. For holomorphic differentiation, pass holomorphic=True.")
    raise TypeError(msg.format(aval.dtype.name))


def hessian(fun, argnums=0, holomorphic=False):
  """Hessian of `fun`.

  Args:
    fun: Function whose Hessian is to be computed.
    argnums: Optional, integer or tuple of integers. Specifies which positional
      argument(s) to differentiate with respect to (default `0`).
    holomorphic: Optional, bool. Indicates whether `fun` is promised to be
      holomorphic. Default False.

  Returns:
    A function with the same arguments as `fun`, that evaluates the Hessian of
    `fun`.

  >>> g = lambda(x): x[0]**3 - 2*x[0]*x[1] - x[1]**6
  >>> print(jax.hessian(g)(jax.numpy.array([1., 2.])))
  [[   6.,   -2.],
   [  -2., -480.]]
  """
  return jacfwd(jacrev(fun, argnums, holomorphic), argnums, holomorphic)

def _std_basis(pytree):
  leaves, _ = tree_flatten(pytree)
  ndim = sum(map(onp.size, leaves))
  # TODO(mattjj): use a symbolic identity matrix here
  dtype = onp.result_type(*leaves)
  flat_basis = onp.eye(ndim, dtype=dtype)
  return _unravel_array_into_pytree(pytree, 1, flat_basis)

def _unravel_array_into_pytree(pytree, axis, arr):
  leaves, treedef = tree_flatten(pytree)
  axis = axis % arr.ndim
  shapes = [arr.shape[:axis] + onp.shape(l) + arr.shape[axis+1:] for l in leaves]
  parts = _split(arr, onp.cumsum(map(onp.size, leaves[:-1])), axis)
  reshaped_parts = [onp.reshape(x, shape) for x, shape in zip(parts, shapes)]
  return tree_unflatten(treedef, reshaped_parts)

def _split(x, indices, axis):
  if isinstance(x, onp.ndarray):
    return onp.split(x, indices, axis)
  else:
    return x.split(indices, axis)

def _dtype(x):
  return canonicalize_dtype(onp.result_type(x))


def vmap(fun, in_axes=0, out_axes=0):
  """Vectorizing map. Creates a function which maps `fun` over argument axes.

  Args:
    fun: Function to be mapped over additional axes.
    in_axes: Specifies which input axes to map over. Normally this is a tuple with
       one axes specification for each function argument. An integer is interpreted
       as a tuple with the same value for all arguments. One argument axes specification
       can be an integer (0 means first dimension), None (means that the dimension is
       broadcasted). If the argument is a tuple of values, then the axes specification
       can be a matching tuple as well.

    out_axes: Specifies which output axes to map over. These may be integers,
      `None`, or (possibly nested) tuples of integers or `None`.

  Returns:
    Batched/vectorized version of `fun` with arguments that correspond to those
    of `fun`, but with extra array axes at positions indicated by `in_axes`, and
    a return value that corresponds to that of `fun`, but with extra array axes
    at positions indicated by `out_axes`.

  For example, we can implement a matrix-matrix product using a vector dot
  product:

  >>> vv = lambda x, y: np.vdot(x, y)  #  ([a], [a]) -> []
  >>> mv = vmap(vv, (0, None), 0)      #  ([b,a], [a]) -> [b]      (b is the mapped axis)
  >>> mm = vmap(mv, (None, 1), 1)      #  ([b,a], [a,c]) -> [b,c]  (c is the mapped axis)

  Here we use `[a,b]` to indicate an array with shape (a,b). Here are some
  variants:

  >>> mv1 = vmap(vv, (0, 0), 0)   #  ([b,a], [b,a]) -> [b]        (b is the mapped axis)
  >>> mv2 = vmap(vv, (0, 1), 0)   #  ([b,a], [a,b]) -> [b]        (b is the mapped axis)
  >>> mm2 = vmap(mv2, (1, 1), 0)  #  ([b,c,a], [a,c,b]) -> [c,b]  (c is the mapped axis)
  """
  docstr = ("Vectorized version of {fun}. Takes similar arguments as {fun} "
            "but with additional array axes over which {fun} is mapped.")

  _check_callable(fun)
  if (not isinstance(in_axes, (list, tuple, type(None), int))
      or not isinstance(out_axes, (list, tuple, type(None), int))):
    msg = ("vmap arguments in_axes and out_axes must each be an integer, None, "
           "or a (nested) tuple of those types, got {} and {} respectively.")
    raise TypeError(msg.format(type(in_axes), type(out_axes)))

  @wraps(fun, docstr=docstr)
  def batched_fun(*args):
    args_flat, in_tree  = tree_flatten(args)
    f = lu.wrap_init(fun)
    flat_fun, out_tree = flatten_fun_nokwargs(f, in_tree)
    out_flat = batching.batch(flat_fun, args_flat, _flatten_axes(in_tree, in_axes),
                              lambda: _flatten_axes(out_tree(), out_axes))
    return tree_unflatten(out_tree(), out_flat)

  return batched_fun

def _flatten_axes(treedef, axis_tree):
  dummy = tree_unflatten(treedef, [object()] * treedef.num_leaves)
  axes = []
  add_leaves = lambda i, x: axes.extend([i] * len(tree_flatten(x)[0]))
  tree_multimap(add_leaves, _replace_nones(axis_tree), dummy)
  axes = [None if a is _none_proxy else a for a in axes]
  return axes

def _replace_nones(tuptree):
  if type(tuptree) in (list, tuple):
    return tuple(map(_replace_nones, tuptree))
  else:
    return tuptree if tuptree is not None else _none_proxy
class _NoneProxy(object): pass
_none_proxy = _NoneProxy()


def pmap(fun, axis_name=None, devices=None, backend=None):
  """Parallel map with support for collectives.

  The purpose of ``pmap`` is to express single-program multiple-data (SPMD)
  programs and execute them in parallel on XLA devices, such as multiple GPUs or
  multiple TPU cores. Semantically it is comparable to ``vmap`` because both
  transformations map a function over array axes, but where ``vmap`` vectorizes
  functions by pushing the mapped axis down into primitive operations, ``pmap``
  instead replicates the function and executes each replica on its own XLA
  device in parallel.

  Another key difference with ``vmap`` is that while ``vmap`` can only express
  pure maps, ``pmap`` enables the use of parallel SPMD collective operations,
  like all-reduce sum.

  The mapped axis size must be less than or equal to the number of XLA devices
  available (unless ``devices`` is specified, see below). For nested ``pmap``
  calls, the product of the mapped axis sizes must be less than or equal to the
  number of XLA devices.

  Args:
    fun: Function to be mapped over argument axes.
    axis_name: Optional, a hashable Python object used to identify the mapped
      axis so that parallel collectives can be applied.
    devices: This is an experimental feature and the API is likely to change.
      Optional, a sequence of Devices to map over. (Available devices can be
      retrieved via jax.devices()). If specified, the length of the sequence
      must be equal to the size of the mapped axis. Nested ``pmap``s with
      ``devices`` specified in either the inner or outer ``pmap`` are not yet
      supported.
    backend: This is an experimental feature and the API is likely to change.
      Optional, a string representing the xla backend. 'cpu','gpu', or 'tpu'.

  Returns:
    A parallelized version of ``fun`` with arguments that correspond to those of
    ``fun`` but each with an additional leading array axis (with equal sizes)
    and with output that has an additional leading array axis (with the same
    size).

  For example, assuming 8 XLA devices are available, ``pmap`` can be used as a
  map along a leading array axes:

  >>> out = pmap(lambda x: x ** 2)(np.arange(8))
  >>> print(out)
  [0, 1, 4, 9, 16, 25, 36, 49]
  >>> x = np.arange(3 * 2 * 2.).reshape((3, 2, 2))
  >>> y = np.arange(3 * 2 * 2.).reshape((3, 2, 2)) ** 2
  >>> out = pmap(np.dot)(x, y)
  >>> print(out)
  [[[    4.     9.]
    [   12.    29.]]
   [[  244.   345.]
    [  348.   493.]]
   [[ 1412.  1737.]
    [ 1740.  2141.]]]

  In addition to expressing pure maps, ``pmap`` can also be used to express
  parallel single-program multiple-data (SPMD) programs that communicate via
  collective operations. For example:

  >>> f = lambda x: x / jax.lax.psum(x, axis_name='i')
  >>> out = pmap(f, axis_name='i')(np.arange(4.))
  >>> print(out)
  [ 0.          0.16666667  0.33333334  0.5       ]
  >>> print(out.sum())
  1.0

  In this example, ``axis_name`` is a string, but it can be any Python object
  with ``__hash__`` and ``__eq__`` defined.

  The argument ``axis_name`` to ``pmap`` names the mapped axis so that
  collective operations, like ``jax.lax.psum``, can refer to it. Axis names are
  important particularly in the case of nested ``pmap`` functions, where
  collectives can operate over distinct axes:

  >>> from functools import partial
  >>> @partial(pmap, axis_name='rows')
  >>> @partial(pmap, axis_name='cols')
  >>> def normalize(x):
  >>>   row_normed = x / jax.lax.psum(x, 'rows')
  >>>   col_normed = x / jax.lax.psum(x, 'cols')
  >>>   doubly_normed = x / jax.lax.psum(x, ('rows', 'cols'))
  >>>   return row_normed, col_normed, doubly_normed
  >>>
  >>> x = np.arange(8.).reshape((4, 2))
  >>> row_normed, col_normed, doubly_normed = normalize(x)
  >>> print(row_normed.sum(0))
  [ 1.  1.]
  >>> print(col_normed.sum(1))
  [ 1.  1.  1.  1.]
  >>> print(doubly_normed.sum((0, 1)))
  1.0

  The ``devices`` argument can be used to specify exactly which devices are used
  to run the parallel computation. For example, the following code defines
  two parallel computations, one which runs on the first six devices and one on
  the remaining two:

  >>> from functools import partial
  >>> @partial(pmap, axis_name='i', devices=jax.devices()[:6])
  >>> def f1(x):
  >>>   return x / jax.lax.psum(x, axis_name='i')
  >>>
  >>> @partial(pmap, axis_name='i', devices=jax.devices()[-2:])
  >>> def f2(x):
  >>>   return jax.lax.psum(x ** 2, axis_name='i')
  >>>
  >>> print(f1(np.arange(6.)))
  [0.         0.06666667 0.13333333 0.2        0.26666667 0.33333333]
  >>> print(f2(np.array([2., 3.])))
  [ 13.  13.]
  """
  _check_callable(fun)
  axis_name = _TempAxisName() if axis_name is None else axis_name

  @wraps(fun)
  def f_pmapped(*args, **kwargs):
    f = lu.wrap_init(fun)
    args, in_tree = tree_flatten((args, kwargs))
    axis_size = _pmap_axis_size(args)
    _check_args(args)
    flat_fun, out_tree = flatten_fun(f, in_tree)
    out = pxla.xla_pmap(flat_fun, *args, axis_name=axis_name, axis_size=axis_size,
                        devices=tuple(devices) if devices is not None else devices,
                        backend=backend)
    return tree_unflatten(out_tree(), out)

  namestr = "pmap({}, axis_name={})".format
  f_pmapped.__name__ = namestr(f_pmapped.__name__, axis_name)
  return f_pmapped

def _pmap_axis_size(xs):
  for x in xs:
    try:
      return x.shape[0]
    except AttributeError:
      pass
  else:
    msg = "pmap got value with no leading axis to map over: {}."
    raise ValueError(msg.format([x for x in xs if not hasattr(x, 'shape')]))

class _TempAxisName(object):
  def __repr__(self):
    return '<axis {}>'.format(hex(id(self)))


def soft_pmap(fun, axis_name=None, backend=None):
  _check_callable(fun)
  axis_name = _TempAxisName() if axis_name is None else axis_name

  @wraps(fun)
  def f_pmapped(*args, **kwargs):
    f = lu.wrap_init(fun)
    args_flat, in_tree = tree_flatten((args, kwargs))
    axis_size = _pmap_axis_size(args_flat)
    _check_args(args_flat)
    flat_fun, out_tree = flatten_fun(f, in_tree)

    chunk_size, leftover = divmod(axis_size, pxla.unmapped_device_count(backend))
    if chunk_size == 0 and leftover:
      return pmap(fun, axis_name, backend)(*args)  # can map directly onto hardware
    elif leftover:
      msg = ("soft_pmap mapped axis size must be divisble by the number of "
             "XLA devices (or be less than or equal to that number), but got "
             "an axis size of {} with {} devices.")
      raise ValueError(msg.format(axis_size, pxla.pxla.unmapped_device_count()))
    num_chunks = axis_size // chunk_size

    reshaped_args = [_reshape_split(num_chunks, x) for x in args_flat]
    soft_mapped_fun = pxla.split_axis(flat_fun, axis_name, chunk_size)
    reshaped_outs = pxla.xla_pmap(soft_mapped_fun, *reshaped_args,
                                  axis_name=axis_name, axis_size=num_chunks,
                                  devices=None, backend=backend)
    outs = [_reshape_merge(out) for out in reshaped_outs]
    return tree_unflatten(out_tree(), outs)

  namestr = "soft_pmap({}, axis_name={})".format
  f_pmapped.__name__ = namestr(f_pmapped.__name__, axis_name)
  return f_pmapped

def _reshape_split(num_chunks, x):
  aval = core.get_aval(x)
  if aval is core.abstract_unit:
    return x
  else:
    return x.reshape((num_chunks, x.shape[0] // num_chunks) + x.shape[1:])

def _reshape_merge(x):
  aval = core.get_aval(x)
  if aval is core.abstract_unit:
    return x
  else:
    return x.reshape((-1,) + x.shape[2:])


def _papply(fun):
  # This function is for testing purposes.
  axis_name = _TempAxisName()

  def papply_fun(*args, **kwargs):
    f = lu.wrap_init(fun)
    args_flat, in_tree = tree_flatten((args, kwargs))
    flat_fun, out_tree = flatten_fun(f, in_tree)
    axis_size = _pmap_axis_size(args_flat)
    out_flat = parallel.papply(flat_fun, axis_name, args_flat, axis_size)
    return tree_unflatten(out_tree(), out_flat)

  return papply_fun, axis_name


def _parallelize(fun):
  axis_name = _TempAxisName()

  def pfun(*args):
    f = lu.wrap_init(fun)
    args_flat, in_tree = tree_flatten(args)
    f, out_tree = flatten_fun_nokwargs(f, in_tree)
    axis_size = _pmap_axis_size(args_flat)

    chunk_size, leftover = divmod(axis_size, pxla.unmapped_device_count())
    if chunk_size == 0 and leftover:
      return pmap(fun, axis_name)(*args)  # can map directly onto hardware
    elif leftover:
      raise ValueError
    num_chunks = axis_size // chunk_size

    reshaped_args = [_reshape_split(num_chunks, x) for x in args_flat]
    f, out_axes = parallel.papply_transform(f, axis_name, axis_size)
    f = pxla.split_axis(f, axis_name, chunk_size)
    outs = pxla.xla_pmap(f, *reshaped_args, axis_name=axis_name,
                         axis_size=num_chunks, devices=None)
    outs = map(_reshape_merge, outs)
    outs = [batching.matchaxis(axis_size, 0, dst, x)
            for dst, x in zip(out_axes(), outs)]
    return tree_unflatten(out_tree(), outs)

  return pfun


def mask(fun, in_shapes, out_shape):
  in_specs, in_shapes_tree = tree_flatten(in_shapes)
  out_specs, out_shapes_tree = tree_flatten(out_shape)

  in_specs = map(masking.parse_spec, in_specs)
  out_specs = map(masking.parse_spec, out_specs)

  unique_ids = collections.defaultdict(object)
  in_specs  = map(partial(_remap_ids, unique_ids), in_specs)
  out_specs = map(partial(_remap_ids, unique_ids), out_specs)

  def wrapped_fun(args, logical_env):
    args_flat, in_tree = tree_flatten(args)
    if in_tree != in_shapes_tree: raise TypeError("pytree mismatch")
    logical_env = {unique_ids[name] : val for name, val in logical_env.items()}
    in_shapes = map(masking.finalize_spec, in_specs, map(onp.shape, args_flat))
    padded_env = _bind_shapes(in_shapes, [x.shape for x in args_flat])
    f = lu.wrap_init(fun)
    flat_fun, out_tree = flatten_fun_nokwargs(f, in_tree)
    outs, out_shapes_ = masking.mask_fun(
        flat_fun, logical_env, padded_env, args_flat, in_shapes)
    if not out_tree() == out_shapes_tree: raise TypeError("pytree mismatch")
    out_shapes = map(masking.finalize_spec, out_specs, map(onp.shape, outs))
    if not out_shapes == list(out_shapes_):
      raise masking.ShapeError
    if not all(onp.shape(out) == masking.eval_shape_expr(padded_env, expr)
               for out, expr in zip(outs, out_shapes)):
      raise masking.ShapeError
    return tree_unflatten(out_tree(), outs)
  return wrapped_fun

def _remap_ids(names, shape_spec):
  ShapeSpec, Poly, Mon = masking.ShapeSpec, masking.Poly, masking.Mon
  mdim = masking.monomorphic_dim
  return ShapeSpec(Poly({Mon({names[id] : deg for id, deg in mon.items()})
                          : coeff for mon, coeff in poly.items()})
                   if poly is not mdim else mdim for poly in shape_spec)

def _bind_shapes(shape_exprs, shapes):
  env = {}
  for shape_expr, shape in zip(shape_exprs, shapes):
    for poly, d in zip(shape_expr, shape):
      if masking.is_constant(poly):
        continue
      else:
        (binder,), = poly  # TODO generalize to handle striding
        if env.setdefault(binder, d) != d: raise masking.ShapeError
  return env


@curry
def shapecheck(in_shapes, out_shape, fun):
  in_shapes, in_tree = tree_flatten(in_shapes)
  in_shapes = map(masking.parse_spec, in_shapes)
  out_shapes, out_tree = tree_flatten(out_shape)
  out_shapes = map(masking.parse_spec, out_shapes)
  flat_fun, out_tree_ = flatten_fun_nokwargs(lu.wrap_init(fun), in_tree)
  out_shapes_ = masking.shapecheck(flat_fun, in_shapes)
  if out_tree != out_tree_(): raise TypeError("pytree mismatch")
  if not all(map(_shape_spec_consistent, out_shapes, out_shapes_)):
    raise masking.ShapeError
  return fun

def _shape_spec_consistent(spec, expr):
  return all(a == b for a, b in zip(spec, expr) if a is not masking.monomorphic_dim)


def jvp(fun, primals, tangents):
  """Computes a (forward-mode) Jacobian-vector product of `fun`.

  Args:
    fun: Function to be differentiated. Its arguments should be arrays, scalars,
      or standard Python containers of arrays or scalars. It should return an
      array, scalar, or standard Python container of arrays or scalars.
    primals: The primal values at which the Jacobian of `fun` should be
      evaluated. Should be a tuple of arrays, scalar, or standard Python
      container thereof. The length of the tuple is equal to the number of
      positional parameters of `fun`.
    tangents: The tangent vector for which the Jacobian-vector product should be
      evaluated. Should be a tuple of arrays, scalar, or standard Python
      container thereof, with the same tree structure and array shapes as
      `primals`.

  Returns:
    A `(primals_out, tangents_out)` pair, where `primals_out` is
    `fun(*primals)`, and `tangents_out` is the Jacobian-vector product of
    `function` evaluated at `primals` with `tangents`. The `tangents_out` value
    has the same Python tree structure and shapes as `primals_out`.

  For example:

  >>> y, v = jax.jvp(jax.numpy.sin, (0.1,), (0.2,))
  >>> print(y)
  0.09983342
  >>> print(v)
  0.19900084
  """
  if not isinstance(fun, lu.WrappedFun):
    fun = lu.wrap_init(fun)

  ps_flat, tree_def = tree_flatten(primals)
  ts_flat, tree_def_2 = tree_flatten(tangents)
  assert tree_def == tree_def_2, (tree_def, tree_def_2)
  flat_fun, out_tree = flatten_fun_nokwargs(fun, tree_def)
  out_primals, out_tangents = ad.jvp(flat_fun).call_wrapped(ps_flat, ts_flat)
  return (tree_unflatten(out_tree(), out_primals),
          tree_unflatten(out_tree(), out_tangents))

def linearize(fun, *primals):
  """Produce a linear approximation to `fun` using `jvp` and partial evaluation.

  Args:
    fun: Function to be differentiated. Its arguments should be arrays, scalars,
      or standard Python containers of arrays or scalars. It should return an
      array, scalar, or standard python container of arrays or scalars.
    primals: The primal values at which the Jacobian of `fun` should be
      evaluated. Should be a tuple of arrays, scalar, or standard Python
      container thereof. The length of the tuple is equal to the number of
      positional parameters of `fun`.

  Returns:
    A pair where the first element is the value of `f(*primals)` and the second
    element is a function that evaluates the (forward-mode) Jacobian-vector
    product of `fun` evaluated at `primals` without re-doing the linearization
    work.

  In terms of values computed, `linearize` behaves much like a curried `jvp`,
  where these two code blocks compute the same values::
    y, out_tangent = jax.jvp(f, (x,), (in_tangent,))

    y, f_jvp = jax.linearize(f, x)
    out_tangent = f_jvp(in_tangent)

  However, the difference is that `linearize` uses partial evaluation so that
  the function `f` is not re-linearized on calls to `f_jvp`. In general that
  means the memory usage scales with the size of the computation, much like in
  reverse-mode. (Indeed, `linearize` has a similar signature to `vjp`!)

  This function is mainly useful if you want to apply `f_jvp` multiple times,
  i.e. to evaluate a pushforward for many different input tangent vectors at the
  same linearization point. Moreover if all the input tangent vectors are known
  at once, it can be more efficient to vectorize using `vmap`, as in::
    pushfwd = partial(jvp, f, (x,))
    y, out_tangents = vmap(pushfwd, out_axes=(None, 0))((in_tangents,))
  By using `vmap` and `jvp` together like this we avoid the stored-linearization
  memory cost that scales with the depth of the computation, which is incurred
  by both `linearize` and `vjp`.

  Here's a more complete example of using `linearize`:

  >>> def f(x): return 3. * np.sin(x) + np.cos(x / 2.)
  ...
  >>> jax.jvp(f, (2.,), (3.,))
  (array(3.2681944, dtype=float32), array(-5.007528, dtype=float32))
  >>> y, f_jvp = jax.linearize(f, 2.)
  >>> print(y)
  3.2681944
  >>> print(f_jvp(3.))
  -5.007528
  >>> print(f_jvp(4.))
  -6.676704
  """
  f = lu.wrap_init(fun)
  primals_flat, in_tree = tree_flatten((primals, {}))
  jaxtree_fun, out_tree = flatten_fun(f, in_tree)
  out_primals, out_pvals, jaxpr, consts = ad.linearize(jaxtree_fun, *primals_flat)
  out_tree = out_tree()
  out_primal_py = tree_unflatten(out_tree, out_primals)
  primal_avals = list(map(core.get_aval, primals_flat))
  lifted_jvp = partial(lift_linearized, jaxpr, primal_avals, consts,
                       (in_tree, out_tree), out_pvals)
  return out_primal_py, lifted_jvp

def lift_linearized(jaxpr, primal_avals, consts, io_tree, out_pvals, *py_args):
  def fun(*tangents):
    tangent_avals = list(map(core.get_aval, tangents))
    for primal_aval, tangent_aval in zip(primal_avals, tangent_avals):
      try:
        core.lattice_join(primal_aval, tangent_aval)
      except TypeError:
        msg = ("linearized function called on tangent values inconsistent with "
               "the original primal values.")
        raise ValueError(msg)
    dummy = (core.unit,) * len(tangents)
    out = eval_jaxpr(jaxpr, consts, (), *(dummy + tangents))
    tangents_out = out[len(out)//2:]
    return tuple(map(pe.merge_pvals, tangents_out, out_pvals))

  return apply_flat_fun(fun, io_tree, *py_args)

def _check_inexact_input_vjp(x):
  aval = core.get_aval(x)
  if not onp.issubdtype(aval.dtype, onp.inexact):
    msg = ("Primal inputs to reverse-mode differentiation must be of float "
           "or complex type, got type {}")
    raise TypeError(msg.format(aval.dtype.name))


def vjp(fun, *primals, **kwargs):
  """Compute a (reverse-mode) vector-Jacobian product of `fun`.

  `grad` is implemented as a special case of `vjp`.

  Args:
    fun: Function to be differentiated. Its arguments should be arrays, scalars,
      or standard Python containers of arrays or scalars. It should return an
      array, scalar, or standard Python container of arrays or scalars.
    primals: A sequence of primal values at which the Jacobian of `fun`
      should be evaluated. The length of `primals` should be equal to the number
      of positional parameters to `fun`. Each primal value should be a tuple of
      arrays, scalar, or standard Python containers thereof.
    has_aux: Optional, bool. Indicates whether `fun` returns a pair where the
     first element is considered the output of the mathematical function to be
     differentiated and the second element is auxiliary data. Default False.

  Returns:
    A `(primals_out, vjpfun)` pair, where `primals_out` is `fun(*primals)`.
    `vjpfun` is a function from a cotangent vector with the same shape as
    `primals_out` to a tuple of cotangent vectors with the same shape as
    `primals`, representing the vector-Jacobian product of `fun` evaluated at
    `primals`.

  >>> def f(x, y):
  ...   return jax.numpy.sin(x), jax.numpy.cos(y)
  ...
  >>> primals, f_vjp = jax.vjp(f, 0.5, 1.0)
  >>> xbar, ybar = f_vjp((-0.7, 0.3))
  >>> print(xbar)
  -0.61430776
  >>> print(ybar)
  -0.2524413
  """
  has_aux = kwargs.pop('has_aux', False)
  assert not kwargs
  if not isinstance(fun, lu.WrappedFun):
    fun = lu.wrap_init(fun)
  primals_flat, in_tree = tree_flatten(primals)
  _check_args(primals_flat)
  tree_map(_check_inexact_input_vjp, primals)
  if not has_aux:
    flat_fun, out_tree = flatten_fun_nokwargs(fun, in_tree)
    out_primal, out_vjp = ad.vjp(flat_fun, primals_flat)
    out_tree = out_tree()
  else:
    flat_fun, out_aux_trees = flatten_fun_nokwargs2(fun, in_tree)
    out_primal, out_vjp, aux = ad.vjp(flat_fun, primals_flat, has_aux=True)
    out_tree, aux_tree = out_aux_trees()
  out_primal_py = tree_unflatten(out_tree, out_primal)
  vjp_py = partial(apply_flat_fun_nokwargs, out_vjp, (out_tree, in_tree))
  if not has_aux:
    return out_primal_py, vjp_py
  else:
    return out_primal_py, vjp_py, tree_unflatten(aux_tree, aux)


def make_jaxpr(fun):
  """Creates a function that produces its jaxpr given example args.

  Args:
    fun: The function whose `jaxpr` is to be computed. Its positional arguments
      and return value should be arrays, scalars, or standard Python containers
      (tuple/list/dict) thereof.

  Returns:
    A wrapped version of `fun` that when applied to example arguments returns a
    jaxpr representation of `fun` on those arguments.

  A `jaxpr` is JAX's intermediate representation for program traces. The `jaxpr`
  language is based on the simply-typed first-order lambda calculus with
  let-bindings. `make_jaxpr` adapts a function to return its `jaxpr`, which we
  can inspect to understand what JAX is doing internally.

  The `jaxpr` returned is a trace of `fun` abstracted to `ShapedArray` level.
  Other levels of abstraction exist internally.

  We do not describe the semantics of the `jaxpr` language in detail here, but
  instead give a few examples.

  >>> def f(x): return jax.numpy.sin(jax.numpy.cos(x))
  >>> print(f(3.0))
  -0.83602184
  >>> jax.make_jaxpr(f)(3.0)
  { lambda  ;  ; a.
    let b = cos a
        c = sin b
    in c }
  >>> jax.make_jaxpr(jax.grad(f))(3.0)
  { lambda b ;  ; a.
    let c = pack a
        (d) = id c
        e = cos d
        f = cos e
        g = mul b f
        h = neg g
        i = sin d
        j = mul h i
        k = pack j
        (l) = id k
    in l }
  """
  _check_callable(fun)

  def pv_like(x):
    aval = xla.abstractify(x)
    return pe.PartialVal((aval, core.unit))

  @wraps(fun)
  def jaxpr_maker(*args, **kwargs):
    wrapped = lu.wrap_init(fun)
    jax_args, in_tree = tree_flatten((args, kwargs))
    jaxtree_fun, out_tree = flatten_fun(wrapped, in_tree)
    pvals = map(pv_like, jax_args)
    jaxpr, _, _ = pe.trace_to_jaxpr(jaxtree_fun, pvals)
    return jaxpr

  jaxpr_maker.__name__ = "make_jaxpr({})".format(jaxpr_maker.__name__)
  return jaxpr_maker


def device_put(x, device_num=0, backend=None):
  return tree_map(lambda y: xla.device_put_p.bind(y, device_num=device_num, backend=backend), x)


# TODO(mattjj): consider revising
def _device_get(x):
  if isinstance(x, core.Tracer):
    return x
  return x.copy()

def device_get(x):
  for y in tree_leaves(x):
    try:
      y.copy_to_host_async()
    except AttributeError:
      pass
  return tree_map(_device_get, x)


def _argnums_partial(f, dyn_argnums, args):
  if isinstance(dyn_argnums, int):
    dyn_argnums = (dyn_argnums,)
  else:
    dyn_argnums = tuple(dyn_argnums)
  fixed_args = tuple([core.unit if i in dyn_argnums else _wrap_hashably(arg)
                      for i, arg in enumerate(args)])
  dyn_args = tuple(args[i] for i in dyn_argnums)
  return _argnums_partial_(f, dyn_argnums, fixed_args), dyn_args

def _wrap_hashably(arg):
  try:
    hash(arg)
  except TypeError:
    return WrapHashably(arg)  # e.g. ndarrays, DeviceArrays
  else:
    return Hashable(arg)

@lu.transformation
def _argnums_partial_(dyn_argnums, fixed_args, *dyn_args, **kwargs):
  args = [None if arg is core.unit else arg.val for arg in fixed_args]
  for i, arg in zip(dyn_argnums, dyn_args):
    args[i] = arg
  ans = yield args, kwargs
  yield ans

def _check_args(args):
  for arg in args:
    if not (isinstance(arg, core.Tracer) or _valid_jaxtype(arg)):
      raise TypeError("Argument '{}' of type {} is not a valid JAX type"
                      .format(arg, type(arg)))

def _valid_jaxtype(arg):
  try:
    xla.abstractify(arg)  # faster than core.get_aval
  except TypeError:
    return False
  else:
    return True


class CustomTransformsFunction(object):
  def __init__(self, fun, prim):
    self.fun = fun
    self.prim = prim
    wraps(fun)(self)

  def __repr__(self):
    return '<jax.custom_transforms function {fun}>'.format(fun=self.__name__)

  def __call__(self, *args):
    # TODO(mattjj): instead of tracing to a jaxpr, use process_call
    args_flat, in_tree = tree_flatten(args)
    flat_fun, out_tree = flatten_fun_nokwargs(lu.wrap_init(self.fun), in_tree)
    in_pvals = [pe.PartialVal((raise_to_shaped(core.get_aval(x)), core.unit))
                for x in args_flat]
    jaxpr, _, consts = pe.trace_to_jaxpr(flat_fun, in_pvals, instantiate=True)
    outs = self.prim.bind(*it.chain(consts, args_flat), jaxpr=jaxpr,
                          in_tree=in_tree, out_tree=out_tree(),
                          num_consts=len(consts))
    return tree_unflatten(out_tree(), outs)

def custom_transforms(fun):
  """Wraps a function so that its transformation behavior can be controlled.

  A primary use case of ``custom_transforms`` is defining custom VJP rules (aka
  custom gradients) for a Python function, while still supporting other
  transformations like ``jax.jit`` and ``jax.vmap``. Custom differentiation
  rules can be supplied using the ``jax.defjvp`` and ``jax.defvjp`` functions.

  The ``custom_transforms`` decorator wraps ``fun`` so that its transformation
  behavior can be overridden, but not all transformation rules need to be
  specified manually. The default behavior is retained for any non-overridden
  rules.

  The function ``fun`` must satisfy the same constraints required for jit
  compilation. In particular the shapes of arrays in the computation of ``fun``
  may depend on the shapes of ``fun``'s arguments, but not their values.
  Value dependent Python control flow is also not yet supported.

  Args:
    fun: a Python callable. Must be functionally pure. Its arguments and return
      value should be arrays, scalars, or (nested) standard Python containers
      (tuple/list/dict) thereof.

  Returns:
    A Python callable with the same input/output and transformation behavior as
    ``fun``, but for which custom transformation rules can be supplied, e.g.
    using ``jax.defvjp``.

  For example:

  >>> @jax.custom_transforms
  ... def f(x):
  ...   return np.sin(x ** 2)
  ...
  >>> print(f(3.))
  0.4121185
  >>> print(jax.grad(f)(3.))
  -5.4667816
  >>> jax.defvjp(f, lambda g, x: g * x)
  >>> print(jax.grad(f)(3.))
  3.0
  """
  name = getattr(fun, '__name__', '<unnamed custom_transforms primitive>')
  fun_p = core.Primitive(name)
  fun_p.multiple_results = True

  def fun_impl(*args, **params):
    consts, args = split_list(args, [params['num_consts']])
    return core.eval_jaxpr(params['jaxpr'], consts, (), *args)
  fun_p.def_impl(fun_impl)

  def fun_jvp(primals, tangents, **params):
    return ad.jvp(lu.wrap_init(fun_impl, params)).call_wrapped(primals, tangents)
  ad.primitive_jvps[fun_p] = fun_jvp

  def fun_batch(args, dims, **params):
    return batching.batch_fun(lu.wrap_init(fun_impl, params), args, dims)
  batching.primitive_batchers[fun_p] = fun_batch

  def fun_abstract_eval(*avals, **params):
    return pe.abstract_eval_fun(fun_impl, *avals, **params)
  fun_p.def_abstract_eval(fun_abstract_eval)

  def fun_translation(c, *xla_args, **params):
    return xla.lower_fun(fun_impl, True)(c, *xla_args, **params)
  xla.translations[fun_p] = fun_translation

  return CustomTransformsFunction(fun, fun_p)

def _check_custom_transforms_type(name, fun):
  if type(fun) is not CustomTransformsFunction:
    msg = ("{} requires a custom_transforms function as its first argument, "
          "but got type {}.")
    raise TypeError(msg.format(name, type(fun)))

def defjvp_all(fun, custom_jvp):
  """Define a custom JVP rule for a ``custom_transforms`` function.

  If ``fun`` represents a function with signature ``a -> b``, then
  ``custom_jvp`` represents a function with signature ``(a, T a) -> (b, T b)``,
  where we use ``T x`` to represent a tangent type for the type ``x``.

  In more detail, ``custom_jvp`` must take two arguments, both tuples of length
  equal to the number of positional arguments to ``fun``. The first argument to
  ``custom_jvp`` represents the input primal values, and the second represents
  the input tangent values. ``custom_jvp`` must return a pair where the first
  element represents the output primal value and the second element represents
  the output tangent value.

  Defining a custom JVP rule also affects the default VJP rule, which is derived
  from the JVP rule automatically via transposition.

  Args:
    fun: a custom_transforms function.
    custom_jvp: a Python callable specifying the JVP rule, taking two tuples as
      arguments specifying the input primal values and tangent values,
      respectively. The tuple elements can be arrays, scalars, or (nested)
      standard Python containers (tuple/list/dict) thereof. The output must be a
      pair representing the primal output and tangent output, which  can be
      arrays, scalars, or (nested) standard Python containers. Must be
      functionally pure.

  Returns:
    None. A side-effect is that ``fun`` is associated with the JVP rule
    specified by ``custom_jvp``.

  For example:

  >>> @jax.custom_transforms
  ... def f(x):
  ...   return np.sin(x ** 2)
  ...
  >>> print(f(3.))
  0.4121185
  >>> out_primal, out_tangent = jax.jvp(f, (3.,), (2.,))
  >>> print(out_primal)
  0.4121185
  >>> print(out_tangent)
  -10.933563
  >>> jax.defjvp_all(f, lambda ps, ts: (np.sin(ps[0] ** 2), 8. * ts[0]))
  >>> out_primal, out_tangent = jax.jvp(f, (3.,), (2.,))
  >>> print(out_primal)
  0.4121185
  >>> print(out_tangent)
  16.0
  """
  _check_custom_transforms_type("defjvp_all", fun)
  def custom_transforms_jvp(primals, tangents, **params):
    num_consts, in_tree = params['num_consts'], params['in_tree']
    _, args_flat = split_list(primals, [num_consts])
    consts_dot, args_dot_flat = split_list(tangents, [num_consts])
    if not all(t is ad_util.zero for t in consts_dot):
      msg = ("Detected differentiation with respect to closed-over values with "
             "custom JVP rule, which isn't supported.")
      raise ValueError(msg)
    args = tree_unflatten(in_tree, args_flat)
    args_dot = tree_unflatten(in_tree, args_dot_flat)
    out, out_dot = custom_jvp(args, args_dot)
    out_flat, out_tree = tree_flatten(out)
    out_dot_flat, out_tree2 = tree_flatten(out_dot)
    if out_tree != out_tree2:
      msg = ("Custom JVP rule returned different tree structures for primals "
             "and tangents, but they must be equal: {} and {}.")
      raise TypeError(msg.format(out_tree, out_tree2))
    return out_flat, out_dot_flat
  ad.primitive_jvps[fun.prim] = custom_transforms_jvp

def defjvp(fun, *jvprules):
  """Definine JVP rules for each argument separately.

  This function is a convenience wrapper around ``jax.defjvp_all`` for
  separately defining JVP rules for each of the function's arguments. This
  convenience wrapper does not provide a mechanism for depending on anything
  other than the function arguments and its primal output value, though
  depending on intermediate results is possible using ``jax.defjvp_all``.

  The signature of each component JVP rule is ``lambda g, ans, *primals: ...``
  where ``g`` represents the tangent of the corresponding positional argument,
  ``ans`` represents the output primal, and ``*primals`` represents all the
  primal positional arguments.

  Defining a custom JVP rule also affects the default VJP rule, which is derived
  from the JVP rule automatically via transposition.

  Args:
    fun: a custom_transforms function.
    *jvprules: a sequence of functions or Nones specifying the JVP rule for each
      corresponding positional argument. When an element is None, it indicates
      that the Jacobian from the corresponding input to the output is zero.

  Returns:
    None. A side-effect is that ``fun`` is associated with the JVP rule
    specified by ``*jvprules``.

  For example:

  >>> @jax.custom_transforms
  ... def f(x):
  ...   return np.sin(x ** 2)
  ...
  >>> print(f(3.))
  0.4121185
  >>> out_primal, out_tangent = jax.jvp(f, (3.,), (2.,))
  >>> print(out_primal)
  0.4121185
  >>> print(out_tangent)
  -10.933563
  >>> jax.defjvp(f, lambda g, ans, x: 8. * g + ans)
  >>> out_primal, out_tangent = jax.jvp(f, (3.,), (2.,))
  >>> print(out_primal)
  0.4121185
  >>> print(out_tangent)
  16.412119
  """
  _check_custom_transforms_type("defjvp", fun)
  def custom_jvp(primals, tangents):
    ans = fun(*primals)
    tangents_out = [rule(t, ans, *primals) for rule, t in zip(jvprules, tangents)
                    if rule is not None and t is not ad_util.zero]
    return ans, reduce(ad.add_tangents, tangents_out, ad_util.zero)
  defjvp_all(fun, custom_jvp)

def defvjp_all(fun, custom_vjp):
  """Define a custom VJP rule for a ``custom_transforms`` function.

  If ``fun`` represents a function with signature ``a -> b``, then
  ``custom_vjp`` represents a function with signature ``a -> (b, CT b -> CT a)``
  where we use ``CT x`` to represent a cotangent type for the type ``x``. That
  is, ``custom_vjp`` should take the same arguments as ``fun`` and return a pair
  where the first element represents the primal value of ``fun`` applied to the
  arguments, and the second element is a VJP function that maps from output
  cotangents to input cotangents, returning a tuple with length equal to the
  number of positional arguments supplied to ``fun``.

  The VJP function returned as the second element of the output of
  ``custom_vjp`` can close over intermediate values computed when evaluating the
  primal value of ``fun``. That is, use lexical closure to share work between
  the forward pass and the backward pass of reverse-mode automatic
  differentiation.

  See also ``jax.custom_gradient``.

  Args:
    fun: a custom_transforms function.
    custom_vjp: a Python callable specifying the VJP rule, taking the same
      arguments as ``fun`` and returning a pair where the first elment is the
      value of ``fun`` applied to the arguments and the second element is a
      Python callable representing the VJP map from output cotangents to input
      cotangents. The returned VJP function must accept a value with the same
      shape as the value of ``fun`` applied to the arguments and must return a
      tuple with length equal to the number of positional arguments to ``fun``.
      Arguments can be arrays, scalars, or (nested) standard Python containers
      (tuple/list/dict) thereof. Must be functionally pure.

  Returns:
    None. A side-effect is that ``fun`` is associated with the VJP rule
    specified by ``custom_vjp``.

  For example:

  >>> @jax.custom_transforms
  ... def f(x):
  ...   return np.sin(x ** 2)
  ...
  >>> print(f(3.))
  0.4121185
  >>> print(jax.grad(f)(3.))
  -5.4667816
  >>> jax.defvjp_all(f, lambda x: (np.sin(x ** 2), lambda g: (g * x,)))
  >>> print(f(3.))
  0.4121185
  >>> print(jax.grad(f)(3.))
  3.0

  An example with a function on two arguments, so that the VJP function must
  return a tuple of length two:

  >>> @jax.custom_transforms
  ... def f(x, y):
  ...   return x * y
  ...
  >>> jax.defvjp_all(f, lambda x, y: (x * y, lambda g: (y, x)))
  >>> print(f(3., 4.))
  12.0
  >>> print(jax.grad(f, argnums=(0, 1))(3., 4.))
  (4.0, 3.0)
  """
  _check_custom_transforms_type("defvjp_all", fun)
  def custom_transforms_vjp(*consts_and_args, **params):
    num_consts, in_tree = params['num_consts'], params['in_tree']
    consts, args_flat = split_list(consts_and_args, [num_consts])
    args = tree_unflatten(params['in_tree'], args_flat)
    out, vjp = custom_vjp(*args)
    out_flat, out_tree = tree_flatten(out)
    assert out_tree == params['out_tree']  # TODO(mattjj): better error message
    def vjp_flat(*cts_flat):
      cts = tree_unflatten(out_tree, cts_flat)
      args_cts_flat, in_tree2 = tree_flatten(vjp(cts))
      assert in_tree == in_tree2  # TODO(mattjj): better error message
      return [core.unit] * num_consts + list(args_cts_flat)
    return out_flat, vjp_flat
  ad.defvjp_all(fun.prim, custom_transforms_vjp)

def defvjp(fun, *vjprules):
  """Define VJP rules for each argument separately.

  This function is a convenience wrapper around ``jax.defvjp_all`` for
  separately defining VJP rules for each of the function's arguments. This
  convenience wrapper does not provide a mechanism for depending on anything
  other than the function arguments and its primal output value, though
  depending on intermediate results is possible using ``jax.defvjp_all``.

  The signature of each component VJP rule is ``lambda g, ans, *primals: ...``
  where ``g`` represents the output cotangent, ``ans`` represents the output
  primal, and ``*primals`` represents all the primal positional arguments.

  Args:
    fun: a custom_transforms function.
    *vjprules: a sequence of functions or Nones specifying the VJP rule for each
      corresponding positional argument. When an element is None, it indicates
      that the Jacobian from the corresponding input to the output is zero.

  Returns:
    None. A side-effect is that ``fun`` is associated with the VJP rule
    specified by ``*vjprules``.

  For example:

  >>> @jax.custom_transforms
  ... def f(x, y):
  ...   return np.sin(x ** 2 + y)
  ...
  >>> print(f(3., 4.))
  0.42016703
  >>> print(jax.grad(f)(3., 4.))
  5.4446807
  >>> print(jax.grad(f, 1)(3., 4.))
  0.9074468
  >>> jax.defvjp(f, None, lambda g, ans, x, y: g + x + y + ans)
  >>> print(jax.grad(f)(3., 4.))
  0.0
  >>> print(jax.grad(f, 1)(3., 4.))
  8.420167
  """
  _check_custom_transforms_type("defvjp", fun)
  def custom_vjp(*primals):
    ans = fun(*primals)
    # TODO(mattjj): avoid instantiating zeros?
    def vjpfun(ct):
      return tuple(vjp(ct, ans, *primals) if vjp else ad_util.zeros_like_jaxval(x)
                   for x, vjp in zip(primals, vjprules))
    return ans, vjpfun
  defvjp_all(fun, custom_vjp)

def custom_gradient(fun):
  """Convenience function for defining custom VJP rules (aka custom gradients).

  While the canonical way to define custom VJP rules is via ``jax.defvjp_all``
  and its convenience wrappers, the ``custom_gradient`` convenience wrapper
  follows TensorFlow's ``tf.custom_gradient`` API. The difference here is that
  ``custom_gradient`` can be used as a decorator on one function that returns
  both the primal value (representing the output of the mathematical function to
  be differentiated) and the VJP (gradient) function.

  See https://www.tensorflow.org/api_docs/python/tf/custom_gradient.

  If the mathematical function to be differentiated has type signature
  ``a -> b``, then the Python callable ``fun`` should have signature
  ``a -> (b, CT b -> CT a)`` where we use ``CT x`` to denote a cotangent type
  for ``x``. See the example below. That is, ``fun`` should return a pair where
  the first element represents the value of the mathematical function to be
  differentiated and the second element is a function that represents the custom
  VJP rule.

  The custom VJP function returned as the second element of the output of ``fun``
  can close over intermediate values computed when evaluating the function to be
  differentiated. That is, use lexical closure to share work between the forward
  pass and the backward pass of reverse-mode automatic differentiation.

  Args:
    fun: a Python callable specifying both the mathematical function to be
      differentiated and its reverse-mode differentiation rule. It should return
      a pair consisting of an output value and a Python callable that represents
      the custom gradient function.

  Returns:
    A Python callable with signature ``a -> b``, i.e. that returns the output
    value specified by the first element of ``fun``'s output pair. A side effect
    is that under-the-hood ``jax.defvjp_all`` is called to set up the returned
    Python callable with the custom VJP rule specified by the second element
    of ``fun``'s output pair.

  For example:

  >>> @jax.custom_gradient
  ... def f(x):
  ...   return x ** 2, lambda g: (g * x,)
  ...
  >>> print(f(3.))
  9.0
  >>> print(jax.grad(f)(3.))
  3.0

  An example with a function on two arguments, so that the VJP function must
  return a tuple of length two:

  >>> @jax.custom_gradient
  ... def f(x, y):
  ...   return x * y, lambda g: (y, x)
  ...
  >>> print(f(3., 4.))
  12.0
  >>> print(jax.grad(f, argnums=(0, 1))(3., 4.))
  (4.0, 3.0)
  """
  def primal_fun(*args, **kwargs):
    ans, _ = fun(*args, **kwargs)
    return ans
  primal_fun = custom_transforms(primal_fun)
  defvjp_all(primal_fun, fun)
  return primal_fun


def jarrett(fun):
  new_fun = custom_transforms(fun)

  def elementwise_jvp(primals, tangents):
    pushfwd = partial(jvp, fun, primals)
    y, jacs = vmap(pushfwd, out_axes=(None, 0))(_elementwise_std_basis(tangents))
    flat_tangents, _ = tree_flatten(tangents)
    out_tangent = sum([t * jac for t, jac in zip(flat_tangents, jacs)])
    return y, out_tangent
  defjvp_all(new_fun, elementwise_jvp)

  return new_fun

def _elementwise_std_basis(pytree):
  leaves, _ = tree_flatten(pytree)
  arity = len(leaves)
  dims = map(onp.size, leaves)
  # TODO(mattjj): use symbolic constants
  dtype = onp.result_type(*leaves)
  if not onp.issubdtype(dtype, onp.floating):
    msg = ("Jacobian only defined for functions with floating input and output "
           "dtypes (i.e. dtypes that model real numbers), got {}.")
    raise TypeError(msg.format(dtype))  # TODO(mattjj, dougalm): handle complex
  basis_array = onp.stack([onp.concatenate(
      [onp.ones(dims[j], dtype) if i == j else onp.zeros(dims[j], dtype)
       for j in range(arity)]) for i in range(arity)])
  return _unravel_array_into_pytree(pytree, 1, basis_array)


# This function mostly exists for making slides about JAX.
def _make_graphviz(fun):
  """Adapts `fun` to return a graphviz dot string of its program representation.

  Args:
    fun: The function whose `jaxpr` is to be rendered into graphviz dot. Its
      positional arguments and return value should be arrays, scalars, or
      standard Python containers (tuple/list/dict) thereof.

  Returns:
    A wrapped version of `fun`, set up to return a graphviz dot string.

  See make_jaxpr for a related function.
  """
  # TODO(mattjj): handle eqn.restructure
  # TODO(mattjj): handle subjaxprs

  def pv_like(x):
    aval = xla.abstractify(x)
    return pe.PartialVal((aval, core.unit))

  id_names = ("id{}".format(i) for i in it.count())

  def jaxpr_to_graphviz(jaxpr, consts):
    fragment = []

    fragment.extend(map(invar_node, jaxpr.invars, jaxpr.invars))
    fragment.extend(map(freevar_node, jaxpr.freevars, jaxpr.freevars))
    fragment.extend(map(constant_node, jaxpr.constvars, consts))

    for eqn in jaxpr.eqns:
      if eqn.destructure:
        id_name = next(id_names)
        fragment.append(function_node(id_name, eqn.primitive.name))
        fragment.extend(edge(invar, id_name) for invar in eqn.invars)
        fragment.extend(edge(id_name, outvar) for outvar in eqn.outvars)
      else:
        fragment.append(function_node(eqn.outvars[0], eqn.primitive.name))
        fragment.extend(edge(invar, eqn.outvars[0]) for invar in eqn.invars)
    fragment.append(outvar_node(jaxpr.outvar, "out"))
    return graph(''.join(fragment))

  edge = '{} -> {} [color=gray30];\n'.format
  function_node = '{} [label="{}", shape=box, color=lightskyblue, style=filled];\n'.format
  invar_node = '{} [rank=2, label="{}", color=mediumspringgreen, style=filled];\n'.format
  outvar_node = '{} [label="{}", fillcolor=indianred1, style="filled,dashed", color=black];\n'.format
  constant_node = '{} [rank=2, label="{}", color=goldenrod1, style=filled];\n'.format
  freevar_node = '{} [rank=2, label="{}", color=palegreen, style=filled];\n'.format
  graph = 'digraph G {{{}}}'.format

  @wraps(fun)
  def graphviz_maker(*args, **kwargs):
    wrapped = lu.wrap_init(fun, kwargs)
    jax_args, in_trees = unzip2(map(pytree_to_jaxtupletree, args))
    jaxtree_fun, out_tree = pytree_fun_to_jaxtupletree_fun(wrapped, in_trees)
    pvals = map(pv_like, jax_args)
    jaxpr, _, consts = pe.trace_to_jaxpr(jaxtree_fun, pvals)
    return jaxpr_to_graphviz(jaxpr, consts)

  graphviz_maker.__name__ = "make_graphviz({})".format(graphviz_maker.__name__)
  return graphviz_maker


class ShapeDtypeStruct(object):
  __slots__ = ["shape", "dtype"]
  def __init__(self, shape, dtype):
    self.shape = shape
    self.dtype = dtype

def eval_shape(fun, *args, **kwargs):
  """Compute the shape/dtype of ``fun(*args, **kwargs)`` without any FLOPs.

  This utility function is useful for performing shape inference. Its
  input/output behavior is defined by:

    def eval_shape(fun, *args, **kwargs):
      out = fun(*args, **kwargs)
      return jax.tree_util.tree_map(shape_dtype_struct, out)

    def shape_dtype_struct(x):
      return ShapeDtypeStruct(x.shape, x.dtype)

    class ShapeDtypeStruct(object):
      __slots__ = ["shape", "dtype"]
      def __init__(self, shape, dtype):
        self.shape = shape
        self.dtype = dtype

  In particular, the output is a pytree of objects that have ``shape`` and
  ``dtype`` attributes, but nothing else about them is guaranteed by the API.

  But instead of applying ``fun`` directly, which might be expensive, it uses
  JAX's abstract interpretation machinery to evaluate the shapes without doing
  any FLOPs.

  Using ``eval_shape`` can also catch shape errors, and will raise same shape
  errors as evaluating ``fun(*args, **kwargs)``.

  Args:
    *args: a positional argument tuple of arrays, scalars, or (nested) standard
      Python containers (tuples, lists, dicts, namedtuples, i.e. pytrees) of
      those types. Since only the ``shape`` and ``dtype`` attributes are
      accessed, only values that duck-type arrays are required, rather than real
      ndarrays. The duck-typed objects cannot be namedtuples because those are
      treated as standard Python containers. See the example below.
    **kwargs: a keyword argument dict of arrays, scalars, or (nested) standard
      Python containers (pytrees) of those types. As in ``args``, array values
      need only be duck-typed to have ``shape`` and ``dtype`` attributes.

  For example:

  >>> f = lambda A, x: np.tanh(np.dot(A, x))
  >>> class MyArgArray(object):
  ...   def __init__(self, shape, dtype):
  ...     self.shape = shape
  ...     self.dtype = dtype
  ...
  >>> A = MyArgArray((2000, 3000), np.float32)
  >>> x = MyArgArray((3000, 1000), np.float32)
  >>> out = jax.eval_shape(f, A, x)  # no FLOPs performed
  >>> print(out.shape)
  (2000, 1000)
  >>> print(out.dtype)
  dtype('float32')
  """
  def abstractify(x):
    return ShapedArray(onp.shape(x), onp.result_type(x))
  args_flat, in_tree = tree_flatten((args, kwargs))
  fun, out_tree = flatten_fun(lu.wrap_init(fun), in_tree)
  out = pe.abstract_eval_fun(fun.call_wrapped, *map(abstractify, args_flat))
  out = [ShapeDtypeStruct(x.shape, x.dtype) for x in out]
  return tree_unflatten(out_tree(), out)


def _custom_implicit_solve(solve, tangent_solve):
  """Define gradients for a function that performs an implicit solve.

  Note: this isn't ready for widespread use yet -- it does not handle closed
  over values inside solve yet.

  Args:
    solve: callable that takes two positional arguments, func and params, and
      returns a solution such that func(params, solution) = 0. In other words,
      the following is assumed to be true (but not checked):
        solution = solve(func, params)
        error = func(solution, params)
        assert tree_all(tree_map(partial(np.allclose, 0.0), error)
    tangent_solve: callable that takes two positional arguments, a linear
      function ``f`` and (possibly nested) array(s) ``y``, and returns a
      solution ``x`` such that ``f(x)=y``:

      - For scalar ``y``, use ``lambda f, y: y / f(1.0)``.
      - For vector ``y``, you could use a linear solve with the Jacobian, if
        dimensionality of ``y`` is not too large:
        ``lambda f, y: np.linalg.solve(jacobian(f)(y), y)``.

  Returns:
    Wrapped version of solve with JVP and VJPs defined with respect to
    ``params`` via implicit differentaion, rather than differntiating through
    the solve.
  """
  @wraps(solve)
  def wrapper(func, params):

    @custom_transforms
    def solve_impl(params):
      return solve(func, params)

    @partial(defjvp_all, solve_impl)
    def solve_impl_jvp(primals, tangents):
      # F(u(m), m) = 0  # system of equations in m
      # ∂_0 F(u(m), m) ∂ u(m) + ∂_1 F(u(m), m) = 0
      # ∂ u(m) = - (∂_0 F(u*, m))^{-1} ∂_1 F(u*, m)
      params, = primals
      grad_params, = tangents
      solution = solve_impl(params)
      unchecked_zeros, f_jvp = vjp(func, solution, params)
      grad_solution = tree_map(
          lambda x: -x,
          tangent_solve(lambda p: f_jvp(p)[0], f_jvp(grad_params)[1])
      )
      return solution, grad_solution

    return solve_impl(params)
  return wrapper
