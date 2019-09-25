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

from collections import namedtuple

import itertools as it

import numpy as onp

import six
from six.moves import reduce

from .. import core
from ..core import Trace, Tracer, new_master
from ..abstract_arrays import ShapedArray, make_shaped_array, array_types, raise_to_shaped
from ..ad_util import add_jaxvals, add_jaxvals_p, zeros_like_jaxval, zeros_like_p
from ..linear_util import transformation, transformation_with_aux, wrap_init
from ..util import unzip2, partial, safe_map
from . import xla
from . import partial_eval as pe

map = safe_map


def batch(fun, in_vals, in_dims, out_dim_dests):
  out_vals, out_dims = batch_fun(fun, in_vals, in_dims)
  size, = {x.shape[d] for x, d in zip(in_vals, in_dims) if d is not not_mapped}
  return map(partial(matchaxis, size), out_dims, out_dim_dests(), out_vals)

def batch_fun(fun, in_vals, in_dims):
  with new_master(BatchTrace) as master:
    fun, out_dims = batch_subtrace(fun, master, in_dims)
    out_vals = fun.call_wrapped(*in_vals)
    del master
  return out_vals, out_dims()

@transformation_with_aux
def batch_subtrace(master, in_dims, *in_vals):
  trace = BatchTrace(master, core.cur_sublevel())
  in_tracers = map(partial(BatchTracer, trace), in_vals, in_dims)
  outs = yield in_tracers, {}
  out_tracers = map(trace.full_raise, outs)
  out_vals, out_dims = unzip2((t.val, t.batch_dim) for t in out_tracers)
  yield out_vals, out_dims


### tracer

# TODO(mattjj): use a special sentinel type rather than None
NotMapped = type(None)
not_mapped = None

class BatchTracer(Tracer):
  __slots__ = ['val', 'batch_dim']

  def __init__(self, trace, val, batch_dim):
    assert core.skip_checks or type(batch_dim) in (int, NotMapped)
    self.trace = trace
    self.val = val
    self.batch_dim = batch_dim

  @property
  def aval(self):
    aval = raise_to_shaped(core.get_aval(self.val))
    if self.batch_dim is not_mapped:
      return aval
    else:
      if aval is core.abstract_unit:
        return aval
      elif type(aval) is ShapedArray:
        assert 0 <= self.batch_dim < aval.ndim
        new_shape = tuple(onp.delete(aval.shape, self.batch_dim))
        return ShapedArray(new_shape, aval.dtype)
      else:
        raise TypeError(aval)

  def full_lower(self):
    if self.batch_dim is not_mapped:
      return core.full_lower(self.val)
    else:
      return self

class BatchTrace(Trace):
  def pure(self, val):
    return BatchTracer(self, val, not_mapped)

  def lift(self, val):
    return BatchTracer(self, val, not_mapped)

  def sublift(self, val):
    return BatchTracer(self, val.val, val.batch_dim)

  def process_primitive(self, primitive, tracers, params):
    vals_in, dims_in = unzip2((t.val, t.batch_dim) for t in tracers)
    if all(bdim is not_mapped for bdim in dims_in):
      return primitive.bind(*vals_in, **params)
    else:
      # TODO(mattjj,phawkins): if no rule implemented, could vmap-via-map here
      batched_primitive = get_primitive_batcher(primitive)
      val_out, dim_out = batched_primitive(vals_in, dims_in, **params)
      if primitive.multiple_results:
        return map(partial(BatchTracer, self), val_out, dim_out)
      else:
        return BatchTracer(self, val_out, dim_out)

  def process_call(self, call_primitive, f, tracers, params):
    assert call_primitive.multiple_results
    if call_primitive in pe.map_primitives:
      return self.process_map(call_primitive, f, tracers, params)
    vals, dims = unzip2((t.val, t.batch_dim) for t in tracers)
    if all(bdim is not_mapped for bdim in dims):
      return call_primitive.bind(f, *vals, **params)
    else:
      f, dims_out = batch_subtrace(f, self.master, dims)
      vals_out = call_primitive.bind(f, *vals, **params)
      return [BatchTracer(self, v, d) for v, d in zip(vals_out, dims_out())]

  def process_map(self, map_primitive, f, tracers, params):
    vals, dims = unzip2((t.val, t.batch_dim) for t in tracers)
    if all(dim is not_mapped for dim in dims):
      return map_primitive.bind(f, *vals, **params)
    else:
      size, = {x.shape[d] for x, d in zip(vals, dims) if d is not not_mapped}
      is_batched = tuple(d is not not_mapped for d in dims)
      vals = [moveaxis(x, d, 1) if d is not not_mapped and d != 1 else x
              for x, d in zip(vals, dims)]
      dims = tuple(not_mapped if d is not_mapped else 0 for d in dims)
      f, dims_out = batch_subtrace(f, self.master, dims)
      vals_out = map_primitive.bind(f, *vals, **params)
      dims_out = tuple(d + 1 if d is not not_mapped else d for d in dims_out())
      return [BatchTracer(self, v, d) for v, d in zip(vals_out, dims_out)]

  def post_process_call(self, call_primitive, out_tracers, params):
    vals, dims = unzip2((t.val, t.batch_dim) for t in out_tracers)
    master = self.master
    def todo(x):
      trace = BatchTrace(master, core.cur_sublevel())
      return map(partial(BatchTracer, trace), x, dims)
    return vals, todo


### primitives

primitive_batchers = {}

def get_primitive_batcher(p):
  try:
    return primitive_batchers[p]
  except KeyError:
    raise NotImplementedError(
        "Batching rule for '{}' not implemented".format(p))

def defvectorized(prim):
  primitive_batchers[prim] = partial(vectorized_batcher, prim)

def vectorized_batcher(prim, batched_args, batch_dims, **params):
  assert all(batch_dims[0] == bd for bd in batch_dims[1:]), batch_dims
  return prim.bind(*batched_args, **params), batch_dims[0]

def defbroadcasting(prim):
  primitive_batchers[prim] = partial(broadcast_batcher, prim)

def broadcast_batcher(prim, args, dims, **params):
  shapes = {(x.shape, d) for x, d in zip(args, dims) if onp.ndim(x)}
  if len(shapes) == 1:
    # if there's only agreeing batch dims and scalars, just call the primitive
    d = next(d for d in dims if d is not not_mapped)
    return prim.bind(*args, **params), d
  else:
    size, = {shape[d] for shape, d in shapes if d is not not_mapped}
    args = [bdim_at_front(x, d, size) for x, d in zip(args, dims)]
    ndim = max(onp.ndim(x) for x in args)  # special-case scalar broadcasting
    args = [_handle_scalar_broadcasting(ndim, x, d) for x, d in zip(args, dims)]
    return prim.bind(*args, **params), 0

def _handle_scalar_broadcasting(nd, x, d):
  if d is not_mapped or nd == onp.ndim(x):
    return x
  else:
    return x.reshape(x.shape + (1,) * (nd - onp.ndim(x)))

def defreducer(prim):
  primitive_batchers[prim] = partial(reducer_batcher, prim)

def reducer_batcher(prim, batched_args, batch_dims, axes, **params):
  operand, = batched_args
  bdim, = batch_dims
  axes = tuple(onp.where(onp.less(axes, bdim), axes, onp.add(axes, 1)))
  bdim_out = int(list(onp.delete(onp.arange(operand.ndim), axes)).index(bdim))
  if 'input_shape' in params:
    params = dict(params, input_shape=operand.shape)
  return prim.bind(operand, axes=axes, **params), bdim_out

# sets up primitive batchers for ad_util and xla primitives

def add_batched(batched_args, batch_dims):
  bdx, bdy = batch_dims
  x, y = batched_args
  if bdx == bdy or core.get_aval(x) == core.abstract_unit:
    return add_jaxvals(x, y), bdx
  elif bdx is not_mapped:
    x = broadcast(x, y.shape[bdy], bdy)
    return add_jaxvals(x, y), bdy
  elif bdy is not_mapped:
    y = broadcast(y, x.shape[bdx], bdx)
    return add_jaxvals(x, y), bdx
  else:
    x = moveaxis(x, bdx, bdy)
    return add_jaxvals(x, y), bdy
primitive_batchers[add_jaxvals_p] = add_batched

def zeros_like_batched(batched_args, batch_dims):
  val, = batched_args
  bdim, = batch_dims
  return zeros_like_jaxval(val), bdim
primitive_batchers[zeros_like_p] = zeros_like_batched

defvectorized(xla.device_put_p)

### util

# These utilities depend on primitives for things like broadcasting, reshaping,
# and transposition on arrays. To avoid a circular import from depending on
# lax.py, these functions use method dispatch on their arguments, which could be
# DeviceArrays, numpy.ndarrays, or traced versions of those. This strategy
# almost works, except for broadcast, for which raw numpy.ndarrays don't have a
# method. To handle that case, the `broadcast` function uses a try/except.

class _Last(object): pass
last = _Last()

def broadcast(x, sz, axis):
  if core.get_aval(x) is core.abstract_unit:
    return core.unit
  if axis is last:
    axis = onp.ndim(x)
  shape = list(onp.shape(x))
  shape.insert(axis, sz)
  if isinstance(x, onp.ndarray) or onp.isscalar(x):
    return onp.broadcast_to(x, shape)
  else:
    broadcast_dims = tuple(onp.delete(onp.arange(len(shape)), axis))
    return x.broadcast_in_dim(shape, broadcast_dims)

def moveaxis(x, src, dst):
  if core.get_aval(x) is core.abstract_unit:
    return core.unit
  src, dst = src % x.ndim, dst % x.ndim
  perm = [i for i in range(onp.ndim(x)) if i != src]
  perm.insert(dst, src)
  return x.transpose(perm)

def matchaxis(sz, src, dst, x):
  if core.get_aval(x) is core.abstract_unit:
    return core.unit
  if src == dst:
    return x
  elif type(src) == type(dst) == int:
    return moveaxis(x, src, dst)
  elif type(src) == int and dst is last:
    return moveaxis(x, src, -1)
  elif src is not_mapped and dst is not not_mapped:
    return broadcast(x, sz, dst)
  else:
    raise ValueError((src, dst))

def bdim_at_front(x, bdim, size):
  if core.get_aval(x) is core.abstract_unit:
    return core.unit
  if bdim is not_mapped:
    return broadcast(x, size, 0)
  else:
    return moveaxis(x, bdim, 0)


def _promote_aval_rank(sz, aval):
  if aval is core.abstract_unit:
    return core.abstract_unit
  else:
    return ShapedArray((sz,) + aval.shape, aval.dtype)

def batch_jaxpr(jaxpr, size, batched, instantiate):
  f = wrap_init(core.jaxpr_as_fun(jaxpr))
  f, batched_out = batched_traceable(f, size, batched, instantiate)
  avals_in = [_promote_aval_rank(size, a) if b else a
              for a, b in zip(jaxpr.in_avals, batched)]
  in_pvals = [pe.PartialVal((aval, core.unit)) for aval in avals_in]
  jaxpr_out, pvals_out, consts_out = pe.trace_to_jaxpr(f, in_pvals, instantiate=True)
  avals_out, _ = unzip2(pvals_out)
  jaxpr_out = core.TypedJaxpr(jaxpr_out, consts_out, avals_in, avals_out)
  return jaxpr_out, batched_out()

@transformation_with_aux
def batched_traceable(size, batched, instantiate, *vals):
  in_dims = [0 if b else None for b in batched]
  with new_master(BatchTrace) as master:
    trace = BatchTrace(master, core.cur_sublevel())
    ans = yield map(partial(BatchTracer, trace), vals, in_dims), {}
    out_tracers = map(trace.full_raise, ans)
    out_vals, out_dims = unzip2((t.val, t.batch_dim) for t in out_tracers)
    del master, out_tracers
  if type(instantiate) is bool:
    instantiate = [instantiate] * len(out_vals)
  out_vals = [moveaxis(x, d, 0) if d is not not_mapped and d != 0
              else broadcast(x, size, 0) if d is not_mapped and inst else x
              for x, d, inst in zip(out_vals, out_dims, instantiate)]
  out_batched = [d is not not_mapped or inst
                 for d, inst in zip(out_dims, instantiate)]
  yield out_vals, out_batched
