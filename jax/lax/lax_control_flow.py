# Copyright 2019 Google LLC
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
Control flow primitives.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import itertools
import operator
import threading

import numpy as onp
import six

from jax import api
from jax import core
from jax.lax import lax
from jax import linear_util as lu
from jax.abstract_arrays import ShapedArray, raise_to_shaped
from jax.api_util import flatten_fun_nokwargs
from jax.interpreters import ad
from jax.interpreters import partial_eval as pe
from jax.interpreters import xla
from jax.interpreters import batching
from jax.interpreters import masking
from jax.lib import xla_bridge as xb
from jax.lib import xla_client
from jax.util import (partial, unzip2, safe_map, safe_zip, split_list,
                      split_dict, cache)
from jax.tree_util import (tree_flatten, tree_unflatten, treedef_is_leaf,
                           treedef_children)
from jax import ad_util

_map = safe_map
zip = safe_zip
_reduce = six.moves.reduce


@cache()
def _initial_style_jaxpr(fun, in_tree, in_avals):
  in_pvals = [pe.PartialVal((aval, core.unit)) for aval in in_avals]
  fun, out_tree = flatten_fun_nokwargs(lu.wrap_init(fun), in_tree)
  jaxpr, out_pvals, consts = pe.trace_to_jaxpr(fun, in_pvals, instantiate=True)
  out_avals = _map(raise_to_shaped, unzip2(out_pvals)[0])
  const_avals = tuple(raise_to_shaped(core.get_aval(c)) for c in consts)
  typed_jaxpr = core.TypedJaxpr(pe.closure_convert_jaxpr(jaxpr),
                                (), const_avals + in_avals, out_avals)
  return typed_jaxpr, consts, out_tree()

def _abstractify(x):
  return raise_to_shaped(core.get_aval(x))

def typecheck(aval, x):
  aval = raise_to_shaped(aval)
  try:
    return aval == core.lattice_join(aval, core.get_aval(x))
  except TypeError:
    return False

def typematch(aval1, aval2):
  return raise_to_shaped(aval1) == raise_to_shaped(aval2)

class FixedPointError(Exception): pass


### fori_loop and while_loop

def fori_loop(lower, upper, body_fun, init_val):
  """Loop from ``lower`` to ``upper`` by reduction to ``while_loop``.

  The type signature in brief is

  .. code-block:: haskell

    fori_loop :: Int -> Int -> ((int, a) -> a) -> a -> a

  The semantics of ``fori_loop`` are given by this Python implementation::

    def fori_loop(lower, upper, body_fun, init_val):
      val = init_val
      for i in range(lower, upper):
        val = body_fun(i, val)
      return val

  Unlike that Python version, ``fori_loop`` is implemented in terms of a call to
  ``while_loop``. See the docstring for ``while_loop`` for more information.

  Args:
    lower: an integer representing the loop index lower bound (inclusive)
    upper: an integer representing the loop index upper bound (exclusive)
    body_fun: function of type ``(int, a) -> a``.
    init_val: initial loop carry value of type ``a``.

  Returns:
    Loop value from the final iteration, of type ``a``.
  """
  def while_cond_fun(loop_carry):
    i, _ = loop_carry
    return lax.lt(i, upper)

  def while_body_fun(loop_carry):
    i, x = loop_carry
    return lax.add(i, lax._const(i, 1)), body_fun(i, x)

  _, result = while_loop(while_cond_fun, while_body_fun, (lower, init_val))
  return result


def while_loop(cond_fun, body_fun, init_val):
  """Call ``body_fun`` repeatedly in a loop while ``cond_fun`` is True.

  The type signature in brief is

  .. code-block:: haskell

    while_loop :: (a -> Bool) -> (a -> a) -> a -> a

  The semantics of ``while_loop`` are given by this Python implementation::

    def while_loop(cond_fun, body_fun, init_val):
      val = init_val
      while cond_fun(val):
        val = body_fun(val)
      return val

  Unlike that Python version, ``while_loop`` is a JAX primitive and is lowered
  to a single XLA While HLO. That makes it useful for reducing compilation times
  for jit-compiled functions, since native Python loop constructs in an ``@jit``
  function are unrolled, leading to large XLA computations.

  Another difference from using Python-native loop constructs is that
  ``while_loop`` is not reverse-mode differentiable because XLA computations
  require static bounds on memory requirements.

  Args:
    cond_fun: function of type ``a -> Bool``.
    body_fun: function of type ``a -> a``.
    init_val: value of type ``a``, a type that can be a scalar, array, or any
      pytree (nested Python tuple/list/dict) thereof, representing the initial
      loop carry value.

  Returns:
    The output from the final iteration of body_fun, of type ``a``.
  """
  init_vals, in_tree = tree_flatten((init_val,))
  init_avals = tuple(_map(_abstractify, init_vals))
  cond_jaxpr, cond_consts, cond_tree = _initial_style_jaxpr(cond_fun, in_tree, init_avals)
  body_jaxpr, body_consts, body_tree = _initial_style_jaxpr(body_fun, in_tree, init_avals)
  if not treedef_is_leaf(cond_tree):
    msg = "cond_fun must return a boolean scalar, but got pytree {}."
    raise TypeError(msg.format(cond_tree))
  if cond_jaxpr.out_avals != [ShapedArray((), onp.bool_)]:
    msg = "cond_fun must return a boolean scalar, but got output type(s) {}."
    raise TypeError(msg.format(cond_jaxpr.out_avals))
  if not treedef_children(in_tree) == [body_tree]:
    msg = "body_fun output pytree structure must match init_val, got {} and {}."
    raise TypeError(msg.format(body_tree, treedef_children(in_tree)[0]))
  outs = while_p.bind(*itertools.chain(cond_consts, body_consts, init_vals),
                      cond_nconsts=len(cond_consts), cond_jaxpr=cond_jaxpr,
                      body_nconsts=len(body_consts), body_jaxpr=body_jaxpr)
  return tree_unflatten(body_tree, outs)

def _while_loop_abstract_eval(*args, **kwargs):
  return kwargs["body_jaxpr"].out_avals

def _while_loop_translation_rule(c, axis_env, *args, **kwargs):
  backend = kwargs.pop('backend', None)
  cond_jaxpr, body_jaxpr, cond_nconsts, body_nconsts = split_dict(
      kwargs, ["cond_jaxpr", "body_jaxpr", "cond_nconsts", "body_nconsts"])
  cond_consts, body_consts, init_vals = split_list(args, [cond_nconsts, body_nconsts])
  batched = bool(cond_jaxpr.out_avals[0].shape)

  # Since jaxprs don't have tuples and have multiple return values, but we need
  # the HLO While loop to take a single tuple input and output a single boolean
  # (for the cond computation) or a single tuple output (for the body
  # computation), we build XLA computations that handle the tuple munging before
  # generating a Call into the computations formed from the jaxprs.

  init_carry = c.Tuple(*(cond_consts + body_consts + init_vals))

  cond_c = xb.make_computation_builder("cond_computation")
  cond_carry = cond_c.ParameterWithShape(c.GetShape(init_carry))
  cond_carry_elts = [cond_c.GetTupleElement(cond_carry, i) for i in range(len(args))]
  x, _, z = split_list(cond_carry_elts, [cond_nconsts, body_nconsts])
  pred, = xla.jaxpr_subcomp(cond_c, cond_jaxpr.jaxpr, backend, axis_env,
                            _map(cond_c.Constant, cond_jaxpr.literals), (), *(x + z))
  if batched:
    scalar = xla_client.Shape.array_shape(onp.dtype(onp.bool_), ())
    or_ = xla.primitive_computation(lax.or_p, scalar, scalar)
    pred = cond_c.Reduce(pred, cond_c.Constant(onp.array(False)), or_,
                         list(range(cond_jaxpr.out_avals[0].ndim)))

  body_c = xb.make_computation_builder("body_computation")
  body_carry = body_c.ParameterWithShape(c.GetShape(init_carry))
  body_carry_elts = [body_c.GetTupleElement(body_carry, i) for i in range(len(args))]
  x, y, z = split_list(body_carry_elts, [cond_nconsts, body_nconsts])
  new_z = xla.jaxpr_subcomp(body_c, body_jaxpr.jaxpr, backend, axis_env,
                            _map(body_c.Constant, body_jaxpr.literals), (), *(y + z))
  if batched:
    body_pred, = xla.jaxpr_subcomp(body_c, cond_jaxpr.jaxpr, backend, axis_env,
                                   _map(body_c.Constant, cond_jaxpr.literals), (), *(x + z))
    new_z = _map(partial(_pred_bcast_select, body_c, body_pred), new_z, z)
    assert _map(body_c.GetShape, new_z) == _map(body_c.GetShape, z) # no broadcast
  new_carry = body_c.Tuple(*itertools.chain(x, y, new_z))

  ans = c.While(cond_c.Build(pred), body_c.Build(new_carry), init_carry)
  ans_elts = [c.GetTupleElement(ans, i) for i in range(len(args))]
  _,  _, z = split_list(ans_elts, [cond_nconsts, body_nconsts])
  return c.Tuple(*z)

def _pred_bcast_select(c, pred, x, y):
  pred_shape = c.GetShape(pred).dimensions()
  x_shape = c.GetShape(x).dimensions()
  y_shape = c.GetShape(y).dimensions()
  assert x_shape == y_shape
  assert pred_shape == x_shape[:len(pred_shape)] == y_shape[:len(pred_shape)]
  bcast_pred = c.BroadcastInDim(pred, x_shape, list(range(len(pred_shape))))
  return c.Select(bcast_pred, x, y)

def _while_loop_batching_rule(args, dims, cond_nconsts, cond_jaxpr,
                              body_nconsts, body_jaxpr):
  size, = {x.shape[d] for x, d in zip(args, dims) if d is not batching.not_mapped}
  orig_batched = [d is not batching.not_mapped for d in dims]
  cconst_bat, bconst_bat, init_bat = split_list(orig_batched, [cond_nconsts, body_nconsts])

  carry_bat = init_bat
  for _ in range(1000):
    batched = bconst_bat + carry_bat
    body_jaxpr_batched, carry_bat_out = batching.batch_jaxpr(
        body_jaxpr, size, batched, instantiate=carry_bat)
    cond_jaxpr_batched, (pred_bat,) = batching.batch_jaxpr(
        cond_jaxpr, size, cconst_bat + carry_bat, instantiate=False)
    carry_bat_out = _map(partial(operator.or_, pred_bat), carry_bat_out)
    if carry_bat_out == carry_bat:
      break
    else:
      carry_bat = carry_bat_out
  else:
    raise FixedPointError

  consts, init = split_list(args, [cond_nconsts + body_nconsts])
  const_dims, init_dims = split_list(dims, [cond_nconsts + body_nconsts])
  new_consts = [batching.moveaxis(x, d, 0) if d is not batching.not_mapped and d != 0
                else x for x, d in zip(consts, const_dims)]
  new_init = [batching.broadcast(x, size, 0) if now_bat and not was_bat
              else batching.moveaxis(x, d, 0) if now_bat else x
              for x, d, was_bat, now_bat in zip(init, init_dims, init_bat, carry_bat)]

  outs = while_p.bind(*(new_consts + new_init),
                      cond_nconsts=cond_nconsts, cond_jaxpr=cond_jaxpr_batched,
                      body_nconsts=body_nconsts, body_jaxpr=body_jaxpr_batched)
  out_bdims = [0 if b else batching.not_mapped for b in carry_bat]
  return outs, out_bdims

while_p = lax.Primitive('while')
while_p.multiple_results = True
while_p.def_impl(partial(xla.apply_primitive, while_p))
while_p.def_abstract_eval(_while_loop_abstract_eval)
xla.initial_style_translations[while_p] = _while_loop_translation_rule
batching.primitive_batchers[while_p] = _while_loop_batching_rule


### cond

def cond(pred, true_operand, true_fun, false_operand, false_fun):
  """Conditionally apply ``true_fun`` or ``false_fun``.

  Has equivalent semantics to this Python implementation::

    def cond(pred, true_operand, true_fun, false_operand, false_fun):
      if pred:
        return true_fun(true_operand)
      else:
        return false_fun(false_operand)
  """
  true_ops, true_tree = tree_flatten((true_operand,))
  true_avals = tuple(_map(_abstractify, true_ops))
  true_jaxpr, true_consts, out_tree = _initial_style_jaxpr(true_fun, true_tree, true_avals)
  false_ops, false_tree = tree_flatten((false_operand,))
  false_avals = tuple(_map(_abstractify, false_ops))
  false_jaxpr, false_consts, out_tree2 = _initial_style_jaxpr(false_fun, false_tree, false_avals)
  if out_tree != out_tree2:
    msg = ("true_fun and false_fun outputs must have identical tree structure, "
           "got {} and {}.")
    raise TypeError(msg.format(out_tree, out_tree2))
  if not all(_map(typematch, true_jaxpr.out_avals, false_jaxpr.out_avals)):
    msg = ("true_fun and false_fun outputs must have identical types, "
           "got {} and {}.")
    raise TypeError(msg.format(true_jaxpr.out_avals, false_jaxpr.out_avals))
  out = cond_p.bind(
      *itertools.chain([pred], true_consts, true_ops, false_consts, false_ops),
      true_jaxpr=true_jaxpr, false_jaxpr=false_jaxpr,
      true_nconsts=len(true_consts), false_nconsts=len(false_consts))
  return tree_unflatten(out_tree, out)

def _cond_abstract_eval(*args, **kwargs):
  return kwargs["true_jaxpr"].out_avals

def _cond_translation_rule(c, axis_env, pred, *args, **kwargs):
  backend = kwargs.pop("backend", None)
  true_jaxpr, false_jaxpr, true_nconsts, false_nconsts = split_dict(
      kwargs, ["true_jaxpr", "false_jaxpr", "true_nconsts", "false_nconsts"])
  true_nops = len(true_jaxpr.in_avals) - true_nconsts
  true_consts, true_ops, false_consts, false_ops = split_list(
      args, [true_nconsts, true_nops, false_nconsts])

  def make_computation(name, jaxpr, op_shape):
    c = xb.make_computation_builder(name)
    op = c.ParameterWithShape(op_shape)
    ops = [c.GetTupleElement(op, i) for i in range(len(jaxpr.in_avals))]
    outs = xla.jaxpr_subcomp(c, jaxpr.jaxpr, backend, axis_env,
                             _map(c.Constant, jaxpr.literals), (), *ops)
    return c.Build(c.Tuple(*outs))

  true_op = c.Tuple(*(true_consts + true_ops))
  true_c = make_computation("true_comp", true_jaxpr, c.GetShape(true_op))

  false_op = c.Tuple(*(false_consts + false_ops))
  false_c = make_computation("false_comp", false_jaxpr, c.GetShape(false_op))

  return c.Conditional(pred, true_op, true_c, false_op, false_c)

def _cond_pred_bcast_select(pred, x, y):
  bcast_pred = lax.broadcast_in_dim(pred, onp.shape(x), list(range(onp.ndim(pred))))
  return lax.select(bcast_pred, x, y)

def _cond_batching_rule(args, dims, true_jaxpr, false_jaxpr, true_nconsts,
                        false_nconsts):
  # TODO: maybe avoid moving arg axes to front if we're promoting to select?
  args = [batching.moveaxis(x, d, 0) if d is not batching.not_mapped and d != 0
          else x for x, d in zip(args, dims)]
  true_nops = len(true_jaxpr.in_avals) - true_nconsts
  (pred,), true_consts, true_ops, false_consts, false_ops = split_list(
      args, [1, true_nconsts, true_nops, false_nconsts])
  size, = {x.shape[d] for x, d in zip(args, dims) if d is not batching.not_mapped}
  orig_bat = [d is not batching.not_mapped for d in dims]
  (pred_bat,), tconst_bat, t_bat, fconst_bat, f_bat = split_list(
    orig_bat, [1, true_nconsts, true_nops, false_nconsts])

  _, true_out_bat = batching.batch_jaxpr(true_jaxpr, size, tconst_bat + t_bat, False)
  _, false_out_bat = batching.batch_jaxpr(false_jaxpr, size, fconst_bat + f_bat, False)
  out_bat = [a or b for a, b in zip(true_out_bat, false_out_bat)]

  true_jaxpr_batched, _ = batching.batch_jaxpr(true_jaxpr, size, tconst_bat + t_bat, out_bat)
  false_jaxpr_batched, _ = batching.batch_jaxpr(false_jaxpr, size, fconst_bat + f_bat, out_bat)

  if pred_bat:
    true_out = core.jaxpr_as_fun(true_jaxpr_batched)(*(true_consts + true_ops))
    false_out = core.jaxpr_as_fun(false_jaxpr_batched)(*(false_consts + false_ops))
    true_out = [batching.broadcast(x, size, 0) if not b else x
                for x, b in zip(true_out, out_bat)]
    false_out = [batching.broadcast(x, size, 0) if not b else x
                 for x, b in zip(false_out, out_bat)]
    return [_cond_pred_bcast_select(pred, t, f)
            for t, f in zip(true_out, false_out)], [0] * len(true_out)
  else:
    out_dims = [0 if b else batching.not_mapped for b in out_bat]
    return cond_p.bind(
      *itertools.chain([pred], true_consts, true_ops, false_consts, false_ops),
      true_jaxpr=true_jaxpr_batched, false_jaxpr=false_jaxpr_batched,
      true_nconsts=len(true_consts), false_nconsts=len(false_consts)), out_dims

cond_p = lax.Primitive('cond')
cond_p.multiple_results = True
cond_p.def_impl(partial(xla.apply_primitive, cond_p))
cond_p.def_abstract_eval(_cond_abstract_eval)
batching.primitive_batchers[cond_p] = _cond_batching_rule
xla.initial_style_translations[cond_p] = _cond_translation_rule


### scan

def scan(f, init, xs):
  """Scan a function over leading array axes while carrying along state.

  The type signature in brief is

  .. code-block:: haskell

    scan :: (c -> a -> (c, b)) -> c -> [a] -> (c, [b])

  where we use [t] here to denote the type t with an additional leading axis.
  That is, if t is an array type then [t] represents the type with an additional
  leading axis, and if t is a pytree (container) type with array leaves then [t]
  represents the type with the same pytree structure and corresponding leaves
  each with an additional leading axis.

  When both ``a`` and ``b`` are array types, the semantics of ``scan`` are given
  by this Python implementation::

    def scan(f, init, xs):
      carry = init
      ys = []
      for x in xs:
        carry, y = f(carry, x)
        ys.append(y)
      return carry, np.stack(ys)

  Unlike that Python version, both ``a`` and ``b`` may be arbitrary pytree
  types, and so multiple arrays can be scanned over at once and produce multiple
  output arrays.

  Also unlike that Python version, ``scan`` is a JAX primitive and is lowered to
  a single XLA While HLO. That makes it useful for reducing compilation times
  for jit-compiled functions, since native Python loop constructs in an ``@jit``
  function are unrolled, leading to large XLA computations.

  Args:
    f: a Python function to be scanned of type ``c -> a -> (c, b)``, meaning
      that ``f`` accepts two arguments where the first is a value of the loop
      carry and the second is a slice of ``xs`` along its leading axis, and that
      ``f`` returns a pair where the first element represents a new value for
      the loop carry and the second represents a slice of the output.
    init: an initial loop carry value of type ``c``, which can be a scalar,
      array, or any pytree (nested Python tuple/list/dict) thereof, representing
      the initial loop carry value.
    xs: the value of type ``[a]`` over which to scan along the leading axis,
      where ``[a]`` can be an array or any pytree (nested Python
      tuple/list/dict) thereof with consistent leading axis sizes.

  Returns:
    A pair of type ``(c, [b])`` where the first element represents the final
    loop carry value and the second element represents the stacked outputs of
    the second output of ``f`` when scanned over the leading axis of the inputs.
  """
  num_carry = len(tree_flatten(init)[0])
  in_flat, in_tree = tree_flatten((init, xs))
  init_flat, xs_flat = in_flat[:num_carry], in_flat[num_carry:]
  try:
    length, = {x.shape[0] for x in xs_flat}
  except AttributeError:
    msg = "scan got value with no leading axis to scan over: {}."
    raise ValueError(msg.format([x for x in xs_flat if not hasattr(x, 'shape')]))
  except ValueError:
    msg = "scan got values with different leading axis sizes: {}."
    raise ValueError(msg.format([x.shape[0] for x in xs_flat]))

  carry_avals = tuple(_map(_abstractify, init_flat))
  x_shapes = [masking.padded_shape_as_value(x.shape[1:]) for x in xs_flat]
  x_dtypes = [x.dtype for x in xs_flat]
  x_avals = tuple(_map(ShapedArray, x_shapes, x_dtypes))
  jaxpr, consts, out_tree = _initial_style_jaxpr(f, in_tree, carry_avals + x_avals)
  carry_avals_out, y_avals = split_list(jaxpr.out_avals, [num_carry])
  if tuple(carry_avals_out) != carry_avals:
    msg = "scan carry output type must match carry input type, got {} and {}."
    raise TypeError(msg.format(tuple(carry_avals_out), carry_avals))
  out = scan_p.bind(*itertools.chain(consts, in_flat),
                    forward=True, length=length, jaxpr=jaxpr,
                    num_consts=len(consts), num_carry=num_carry,
                    linear=(False,) * (len(consts) + len(in_flat)))
  return tree_unflatten(out_tree, out)

def _scan_impl(*args, **kwargs):
  forward, length, num_consts, num_carry, jaxpr, linear = split_dict(
      kwargs, ["forward", "length", "num_consts", "num_carry", "jaxpr", "linear"])

  consts, init, xs = split_list(args, [num_consts, num_carry])
  _, _, x_avals = split_list(jaxpr.in_avals, [num_consts, num_carry])
  _, y_avals = split_list(jaxpr.out_avals, [num_carry])

  def body_fun(i, vals):
    i = i if forward else length - i - 1
    carry, ys = split_list(vals, [num_carry])
    x = _map(partial(_index_array, i), x_avals, xs)
    out_flat = core.jaxpr_as_fun(jaxpr)(*(consts + carry + x))
    carry_out, y_updates = split_list(out_flat, [num_carry])
    ys_out = _map(partial(_update_array, i), y_avals, ys, y_updates)
    return carry_out + ys_out

  ys_init = _map(partial(_empty_array, length), y_avals)
  return fori_loop(0, length, body_fun, init + ys_init)

def _index_array(i, aval, x):
  if aval is core.abstract_unit:
    return core.unit
  else:
    return lax.dynamic_index_in_dim(x, i, keepdims=False)

def _empty_array(sz, aval):
  if aval is core.abstract_unit:
    return core.unit
  else:
    return lax.full((sz,) + aval.shape, 0, aval.dtype)

def _update_array(i, aval, xs, x):
  if aval is core.abstract_unit:
    return core.unit
  else:
    return lax.dynamic_update_index_in_dim(xs, x, i, 0)

def _scan_jvp(primals, tangents, forward, length, jaxpr, num_consts, num_carry,
              linear):
  num_xs = len(jaxpr.in_avals) - num_carry - num_consts
  num_ys = len(jaxpr.out_avals) - num_carry
  nonzeros = [t is not ad_util.zero for t in tangents]
  const_nz, init_nz, xs_nz = split_list(nonzeros, [num_consts, num_carry])

  carry_nz = init_nz
  for _ in range(1000):
    nonzeros = const_nz + carry_nz + xs_nz
    jaxpr_jvp, nonzeros_out = ad.jvp_jaxpr(
        jaxpr, nonzeros, instantiate=carry_nz + [False] * num_ys)
    carry_nz_out, ys_nz = nonzeros_out[:num_carry], nonzeros_out[num_carry:]
    if carry_nz_out == carry_nz:
      break
    else:
      carry_nz = carry_nz_out
  else:
    raise FixedPointError
  tangents = [ad.instantiate_zeros(x, t) if t is ad_util.zero and nz else t
              for x, t, nz in zip(primals, tangents, nonzeros)]

  consts, init, xs = split_list(primals, [num_consts, num_carry])
  all_tangents = split_list(tangents, [num_consts, num_carry])
  consts_dot, init_dot, xs_dot = _map(_prune_zeros, all_tangents)

  jaxpr_jvp_rearranged = ad.rearrange_binders(
      jaxpr_jvp,
      [num_consts, num_carry, num_xs], [len(consts_dot), len(init_dot), len(xs_dot)],
      [num_carry, num_ys], [len(init_dot), sum(nonzeros_out) - len(init_dot)])

  consts_linear, init_linear, xs_linear = split_list(linear, [num_consts, num_carry])
  jaxpr_jvp_linear = (consts_linear + [True] * len(consts_dot)
                      + init_linear + [True] * len(init_dot)
                      + xs_linear + [True] * len(xs_dot))

  out_flat = scan_p.bind(
      *(consts + consts_dot + init + init_dot + xs + xs_dot),
      forward=forward, length=length, jaxpr=jaxpr_jvp_rearranged,
      num_consts=num_consts+len(consts_dot), num_carry=num_carry+len(init_dot),
      linear=jaxpr_jvp_linear)

  carry, carry_dot, ys, ys_dot = split_list(out_flat, [num_carry, len(init_dot), num_ys])
  primals_out = carry + ys
  tangents_out = iter(carry_dot + ys_dot)
  tangents_out = [next(tangents_out) if nz else ad_util.zero for nz in nonzeros_out]
  return primals_out, tangents_out

def _prune_zeros(ts):
  return [t for t in ts if t is not ad_util.zero]

def _scan_partial_eval(trace, *tracers, **kwargs):
  forward, length, num_consts, num_carry, jaxpr, linear = split_dict(
      kwargs, ["forward", "length", "num_consts", "num_carry", "jaxpr", "linear"])
  num_xs = len(jaxpr.in_avals) - num_carry - num_consts
  num_ys = len(jaxpr.out_avals) - num_carry

  unknowns = original_unknowns = [t.pval[0] is not None for t in tracers]
  const_uk, init_uk, xs_uk = split_list(unknowns, [num_consts, num_carry])

  carry_uk = init_uk
  for _ in range(1000):
    unknowns = const_uk + carry_uk + xs_uk
    jaxpr_1, jaxpr_2, out_uk = pe.partial_eval_jaxpr(
        jaxpr, unknowns, instantiate=carry_uk + [False] * num_ys)
    carry_uk_out, ys_uk = out_uk[:num_carry], out_uk[num_carry:]
    if carry_uk_out == carry_uk:
      break
    else:
      carry_uk = carry_uk_out
  else:
    raise FixedPointError

  in_consts = [core.unit if uk else t.pval[1] for uk, t in zip(unknowns, tracers)]
  new_tracers = [trace.instantiate_const(t) if uk else trace.new_instantiated_literal(core.unit)
                 for uk, t in zip(unknowns, tracers)]

  carry_avals, y_avals = split_list(jaxpr.out_avals, [num_carry])
  ys_avals = _map(partial(_promote_aval_rank, length), y_avals)
  out_avals = carry_avals + ys_avals
  out_pvs = [aval if uk else None for aval, uk in zip(out_avals, out_uk)]

  linear_1 = [lin or uk for uk, lin in zip(unknowns, linear)]
  out_flat = scan_p.bind(
      *in_consts, forward=forward, length=length, jaxpr=jaxpr_1,
      num_consts=num_consts, num_carry=num_carry, linear=linear_1)
  out_carry, ys, residuals = split_list(out_flat, [num_carry, num_ys])
  out_consts = out_carry + ys
  residual_tracers = _map(trace.new_instantiated_const, residuals)
  out_tracers = [pe.JaxprTracer(trace, pe.PartialVal((pv, const)), None)
                 for pv, const in zip(out_pvs, out_consts)]
  linear_2 = ([lin or not uk for uk, lin in zip(unknowns, linear)]
              + [False] * len(residual_tracers))
  eqn = pe.new_jaxpr_eqn(new_tracers + residual_tracers, out_tracers, scan_p,
                         (), dict(forward=forward, length=length, jaxpr=jaxpr_2,
                                  num_consts=num_consts, num_carry=num_carry,
                                  linear=linear_2))
  for t in out_tracers: t.recipe = eqn
  return out_tracers

def _promote_aval_rank(sz, aval):
  if aval is core.abstract_unit:
    return core.abstract_unit
  else:
    return ShapedArray((sz,) + aval.shape, aval.dtype)

def _scan_transpose(cts, *args, **kwargs):
  forward, length, num_consts, num_carry, jaxpr, linear = split_dict(
      kwargs, ["forward", "length", "num_consts", "num_carry", "jaxpr", "linear"])

  # we can only transpose scans for which the nonlinear values appear in xs
  consts_lin, init_lin, xs_lin = split_list(linear, [num_consts, num_carry])
  num_lin = sum(xs_lin)
  if not all(consts_lin) or not all(init_lin) or not all(xs_lin[:num_lin]):
    raise NotImplementedError

  consts, init, xs, res = split_list(args, [num_consts, num_carry, num_lin])
  assert not any(r is ad.undefined_primal for r in res)

  carry_avals, y_avals = split_list(jaxpr.out_avals, [num_carry])
  ys_avals = _map(partial(_promote_aval_rank, length), y_avals)
  ct_carry, ct_ys = split_list(cts, [num_carry])
  ct_carry = _map(ad.instantiate_zeros_aval, carry_avals, ct_carry)
  ct_ys = _map(ad.instantiate_zeros_aval, ys_avals, ct_ys)
  ct_consts = _map(ad_util.zeros_like_aval, jaxpr.in_avals[:num_consts])

  #       jaxpr :: [T d] -> [T c] -> [T a, res] -> ([T c], [T b])
  # jaxpr_trans :: [] -> [CT d, CT c] -> [CT b, res] -> ([CT d, CT c], [CT a])
  jaxpr_trans = _transpose_jaxpr(num_consts, len(res), jaxpr)
  linear_trans = ([True] * (len(ct_consts) + len(ct_carry) + len(ct_ys))
                  + [False] * len(res))

  outs = scan_p.bind(
      *(ct_consts + ct_carry + ct_ys + res), forward=not forward, length=length,
      jaxpr=jaxpr_trans, num_consts=0, num_carry=num_consts+num_carry,
      linear=linear_trans)
  ct_consts, ct_init, ct_xs = split_list(outs, [num_consts, num_carry])
  return ct_consts + ct_init + ct_xs + [None] * len(res)

# transpose_jaxpr :: ([c, a, res] -> b) -> ([CT c, CT b, res] -> [CT c, CT a]
def _transpose_jaxpr(num_c, num_res, jaxpr):
  num_a = len(jaxpr.in_avals) - num_c - num_res
  c_avals, a_avals, res_avals = split_list(jaxpr.in_avals, [num_c, num_a])
  num_b = len(jaxpr.out_avals)
  b_avals = list(jaxpr.out_avals)

  @lu.wrap_init
  def transposed(*cbar_bbar_res):
    c_bar, b_bar, res = split_list(cbar_bbar_res, [num_c, num_b])
    primals = [ad.undefined_primal] * (num_c + num_a) + res
    _, cbar_abar = ad.backward_pass(jaxpr.jaxpr, jaxpr.literals, (), primals,
                                    b_bar)
    new_c_bar, a_bar, _ = split_list(cbar_abar, [num_c, num_a])
    a_bar = _map(ad.instantiate_zeros_aval, a_avals, a_bar)
    c_bar = _map(ad.instantiate_zeros_aval, c_avals,
                _map(ad.add_tangents, c_bar, new_c_bar))
    return c_bar + a_bar
  return _make_typed_jaxpr(transposed, c_avals + b_avals + res_avals)

def _make_typed_jaxpr(traceable, in_avals):
  pvals = [pe.PartialVal((aval, core.unit)) for aval in in_avals]
  jaxpr, pvals_out, consts = pe.trace_to_jaxpr(traceable, pvals, instantiate=True)
  out_avals, _ = unzip2(pvals_out)
  return core.TypedJaxpr(jaxpr, consts, in_avals, out_avals)


def _scan_batching_rule(args, dims, forward, length, jaxpr, num_consts,
                        num_carry, linear):
  num_ys = len(jaxpr.out_avals) - num_carry
  size, = {x.shape[d] for x, d in zip(args, dims) if d is not batching.not_mapped}
  orig_batched = [d is not batching.not_mapped for d in dims]
  const_batched, init_batched, xs_batched = split_list(orig_batched, [num_consts, num_carry])

  carry_batched = init_batched
  for _ in range(1000):
    batched = const_batched + carry_batched + xs_batched
    jaxpr_batched, batched_out = batching.batch_jaxpr(
        jaxpr, size, batched, instantiate=carry_batched + [False] * num_ys)
    carry_batched_out, ys_batched = batched_out[:num_carry], batched_out[num_carry:]
    if carry_batched_out == carry_batched:
      break
    else:
      carry_batched = carry_batched_out
  else:
    raise FixedPointError

  consts, init, xs = split_list(args, [num_consts, num_carry])
  consts_bdims, init_bdims, xs_bdims = split_list(dims, [num_consts, num_carry])
  new_consts = [batching.moveaxis(x, d, 0) if d is not batching.not_mapped and d != 0
                else x for x, d in zip(consts, consts_bdims)]
  new_init = [batching.broadcast(x, size, 0) if now_batched and not was_batched
              else batching.moveaxis(x, d, 0) if now_batched else x
              for x, d, was_batched, now_batched in
              zip(init, init_bdims, init_batched, carry_batched)]
  new_xs = [batching.moveaxis(x, d, 1) if d is not batching.not_mapped and d != 1
            else x for x, d in zip(xs, xs_bdims)]
  new_args = new_consts + new_init + new_xs

  outs = scan_p.bind(*new_args, forward=forward, length=length, jaxpr=jaxpr_batched,
                     num_consts=num_consts, num_carry=num_carry, linear=linear)
  carry_bdims = [0 if b else batching.not_mapped for b in carry_batched]
  ys_bdims = [1 if b else batching.not_mapped for b in ys_batched]
  return outs, carry_bdims + ys_bdims

def _scan_polymorphic_shape_rule(shape_exprs, forward, length, jaxpr,
                                 num_consts, num_carry, linear):
  const_shexprs, init_shexprs, xs_shexprs = split_list(shape_exprs, [num_consts, num_carry])
  _, y_avals = split_list(jaxpr.out_avals, [num_carry])
  ys_shapes = [ShapeExpr(length, *y_aval.shape) for y_aval in y_avals]
  return init_shexprs + ys_shapes

def _scan_masking_rule(shape_envs, padded_vals, shape_exprs, forward, length,
                       jaxpr, num_consts, num_carry, linear):
  out_shape = _scan_polymorphic_shape_rule(shape_exprs, forward, length, jaxpr,
                                           num_consts, num_carry, linear)
  dynamic_length = masking.eval_dim_expr(shape_envs.logical, length)
  masked_jaxpr = _masked_scan_jaxpr(jaxpr, num_consts, num_carry)
  consts, init, xs = split_list(padded_vals, [num_consts, num_carry])
  max_length, = {x.shape[0] for x in xs}
  const_linear, init_linear, xs_linear = split_list(linear, [num_consts, num_carry])
  out_vals = scan_p.bind(
      *itertools.chain([dynamic_length] + consts, [0], init, xs),
      forward=forward, length=max_length, jaxpr=masked_jaxpr,
      num_consts=1 + num_consts, num_carry=1 + num_carry,
      linear=[False] + const_linear + [False] + init_linear + xs_linear)
  return out_vals[1:], out_shape

def _masked_scan_jaxpr(jaxpr, num_consts, num_carry):
  fun = core.jaxpr_as_fun(jaxpr)

  @lu.wrap_init
  def masked(*args):
    [dynamic_length], consts, [i], carry, xs = split_list(
        args, [1, num_consts, 1, num_carry])
    out = fun(*(consts + carry + xs))
    new_carry, ys = split_list(out, [num_carry])
    new_carry = [lax.select(i < dynamic_length, new_c, c)
                 for new_c, c in zip(new_carry, carry)]
    return [i + 1] + new_carry + ys

  aval = ShapedArray((), onp.int64)
  const_avals, carry_avals, x_avals = split_list(jaxpr.in_avals, [num_consts, num_carry])
  return _make_typed_jaxpr(masked, [aval] + const_avals + [aval] + carry_avals + x_avals)

def scan_bind(*args, **kwargs):
  forward, length, num_consts, num_carry, jaxpr, linear = split_dict(
      kwargs, ["forward", "length", "num_consts", "num_carry", "jaxpr", "linear"])
  consts, init, xs = split_list(args, [num_consts, num_carry])
  assert len(linear) == len(args)

  # check that args match input types
  consts_avals, init_avals, x_avals = split_list(jaxpr.in_avals, [num_consts, num_carry])
  xs_avals = _map(partial(_promote_aval_rank, length), x_avals)
  assert all(_map(typecheck, consts_avals, consts))
  assert all(_map(typecheck, init_avals, init))
  # assert all(_map(typecheck, xs_avals, xs))
  # check that output carry type matches input carry type
  carry_avals, _ = split_list(jaxpr.out_avals, [num_carry])
  assert all(_map(typematch, init_avals, carry_avals))

  # check that the data flow is sensible
  core.check_jaxpr(jaxpr.jaxpr)

  return core.Primitive.bind(scan_p, *args, forward=forward, length=length,
                             jaxpr=jaxpr, num_consts=num_consts,
                             num_carry=num_carry, linear=linear)

scan_p = core.Primitive("scan")
scan_p.multiple_results = True
scan_p.def_custom_bind(scan_bind)
scan_p.def_impl(_scan_impl)
ad.primitive_jvps[scan_p] = _scan_jvp
ad.primitive_transposes[scan_p] = _scan_transpose
pe.custom_partial_eval_rules[scan_p] = _scan_partial_eval
xla.initial_style_translations[scan_p] = xla.lower_fun(_scan_impl, initial_style=True)
batching.primitive_batchers[scan_p] = _scan_batching_rule
masking.shape_parameterized_primitive_rules[scan_p] = _scan_masking_rule


def map(f, xs):
  """Map a function over leading array axes.

  Like Python's builtin map, except inputs and outputs are in the form of
  stacked arrays. Consider using the ``jax.vmap`` transform instead, unless you
  need to apply a function element by element for reduced memory usage or
  heterogeneous computation with other control flow primitives.

  When ``xs`` is an array type, the semantics of ``map`` are given by this
  Python implementation::

    def map(f, xs):
      return np.stack([f(x) for x in xs])

  Like ``scan``, ``map`` is implemented in terms of JAX primitives so many of
  the same advantages over a Python loop apply: ``xs`` may be an arbitrary
  nested pytree type, and the mapped computation is compiled only once.

  Args:
    f: a Python function to apply element-wise over the first axis or axes of
      ``xs``.
    xs: values over which to map along the leading axis.

  Returns:
    Mapped values.
  """
  g = lambda _, x: ((), f(x))
  _, ys = scan(g, (), xs)
  return ys


def _concat_masking_rule(padded_vals, logical_shapes, dimension, operand_shapes):
  del operand_shapes  # Unused.
  result = lax.concatenate(padded_vals, dimension)  # fragmented
  offset = 0
  for padded_val, logical_shape in zip(padded_vals, logical_shapes):
    result = _memcpy(dimension, logical_shape[dimension], padded_val,
                     result, offset)
    offset = offset + logical_shape[dimension]
  return result

def _memcpy(axis, num, src, dst, offset):
  def body(i, dst):
    update = lax.dynamic_index_in_dim(src, i, axis)
    return lax.dynamic_update_index_in_dim(dst, update, i + offset, axis)
  return fori_loop(0, num, body, dst)

masking.masking_rules[lax.concatenate_p] = _concat_masking_rule
