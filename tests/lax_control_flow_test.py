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
from functools import partial
import itertools
from unittest import SkipTest

from absl.testing import absltest
from absl.testing import parameterized

import numpy as onp
import numpy.random as npr

from jax import api
from jax import core
from jax import lax
from jax import random
from jax import test_util as jtu
from jax.util import unzip2
from jax.lib import xla_bridge
import jax.numpy as np  # scan tests use numpy

def scan_reference(f, init, xs):
  carry = init
  ys = []
  for x in xs:
    (carry, y) = f(carry, x)
    ys.append(lax.reshape(y, (1,) + onp.shape(y)))
  ys = lax.concatenate(ys, 0)
  return carry, ys


class LaxControlFlowTest(jtu.JaxTestCase):

  def testWhileWithTuple(self):
    limit = 10

    def loop_cond(state):
      pos, _ = state
      return lax.lt(pos, limit)

    def loop_body(state):
      pos, count = state
      return (lax.add(pos, 1), lax.add(count, 1))

    def loop(init):
      result = lax.while_loop(loop_cond, loop_body, (init, 0))
      _, count = result
      return count

    cloop = api.jit(loop)

    self.assertEqual(loop(2), limit - 2)
    self.assertEqual(cloop(2), limit - 2)
    self.assertEqual(cloop(2), limit - 2)
    self.assertEqual(cloop(3), limit - 3)

  def testNestedWhile(self):

    def outer_loop(num):  # pylint: disable=missing-docstring
      def cond_fun(state):
        num, i, _ = state
        return lax.lt(i, num)

      def body_fun(state):
        num, i, count = state
        return (num, lax.add(i, 1), inner_loop(i, count))

      init_val = (num, 0, 0)
      _, i, count = lax.while_loop(cond_fun, body_fun, init_val)
      return (i, count)

    def inner_loop(i, count):  # pylint: disable=missing-docstring
      def cond_fun(state):
        i, j, _ = state
        return lax.le(j, i)

      def body_fun(state):
        i, j, count = state
        return (i, lax.add(j, 1), lax.add(count, 1))

      init_val = (i, 0, count)
      _, _, count = lax.while_loop(cond_fun, body_fun, init_val)
      return count

    cloop = api.jit(outer_loop)

    self.assertEqual(outer_loop(3), (3, 6))
    self.assertEqual(cloop(3), (3, 6))
    self.assertEqual(cloop(3), (3, 6))
    self.assertEqual(cloop(2), (2, 3))
    self.assertEqual(cloop(4), (4, 10))

  def testWhileWithClosure(self):

    def loop(init, local_limit, inc):

      def loop_cond(state):
        pos, _ = state
        return lax.lt(pos, local_limit)

      def loop_body(state):
        effect[0] = True
        pos, count = state
        return (lax.add(pos, 1), lax.add(count, inc))

      result = lax.while_loop(loop_cond, loop_body, (init, 0))
      _, count = result
      return count

    cloop = api.jit(loop)

    limit = 10
    effect = [False]
    self.assertEqual(loop(2, limit, 1), limit - 2)
    assert effect[0]
    effect[0] = False
    self.assertEqual(cloop(2, limit, 1), limit - 2)
    assert effect[0]
    effect[0] = False
    self.assertEqual(cloop(2, limit, 1), limit - 2)
    self.assertEqual(cloop(3, limit, 1), limit - 3)
    assert not effect[0]

  def testWhileWithClosureJit(self):

    def loop(init, local_limit, inc):

      def loop_cond(state):
        pos, _ = state
        return lax.lt(pos, local_limit)

      def loop_body(state):
        effect[0] = True
        pos, count = state
        f = lambda pos, inc: (lax.add(pos, 1), lax.add(count, inc))
        return api.jit(f)(pos, inc)

      result = lax.while_loop(loop_cond, loop_body, (init, 0))
      _, count = result
      return count

    cloop = api.jit(loop)

    limit = 10
    effect = [False]
    self.assertEqual(loop(2, limit, 1), limit - 2)
    assert effect[0]
    effect[0] = False
    self.assertEqual(cloop(2, limit, 1), limit - 2)
    assert effect[0]
    effect[0] = False
    self.assertEqual(cloop(2, limit, 1), limit - 2)
    self.assertEqual(cloop(3, limit, 1), limit - 3)
    assert not effect[0]

  def testNestedWhileWithDynamicUpdateSlice(self):
    num = 5

    def update_entry(arr, val, i, j):
      val = lax.reshape(val, [1, 1])
      return lax.dynamic_update_slice(arr, val, (i, j))

    def outer_loop(arr):  # pylint: disable=missing-docstring

      def cond_fun(state):
        i, num, _, _ = state
        return lax.lt(i, num)

      def body_fun(state):
        i, num, arr, out = state
        return (lax.add(i, 1), num, arr, inner_loop(i, arr, out))

      out = onp.zeros(arr.shape, dtype=arr.dtype)
      init_val = (0, num, arr, out)
      _, _, _, out = lax.while_loop(cond_fun, body_fun, init_val)
      return out

    def inner_loop(i, arr, out):  # pylint: disable=missing-docstring

      def cond_fun(state):
        i, j, _, _ = state
        return lax.le(j, i)

      def body_fun(state):
        i, j, arr, out = state
        arr_i = lax.dynamic_index_in_dim(arr, i, 0, False)
        arr_i_j = lax.dynamic_index_in_dim(arr_i, j, 0, False)
        out = update_entry(out, arr_i_j, i, j)
        return (i, lax.add(j, 1), arr, out)

      init_val = (i, 0, arr, out)
      _, _, _, out = lax.while_loop(cond_fun, body_fun, init_val)
      return out

    cloop = api.jit(outer_loop)
    arr = npr.RandomState(0).randn(5, 5)
    self.assertAllClose(outer_loop(arr), onp.tril(arr), check_dtypes=False)
    self.assertAllClose(cloop(arr), onp.tril(arr), check_dtypes=False)
    self.assertAllClose(cloop(arr), onp.tril(arr), check_dtypes=False)

  def testLoopWithConjunctionCondition(self):
    def sum_first_n(arr, num):  # pylint: disable=missing-docstring
      def cond_fun(state):
        arr, num, i, _ = state
        return lax.bitwise_and(lax.lt(i, num), lax.lt(i, arr.shape[0]))

      def body_fun(state):
        arr, num, i, total = state
        arr_i = lax.dynamic_index_in_dim(arr, i, 0, False)
        return (arr, num, lax.add(i, 1), lax.add(total, arr_i))

      init_val = (arr, num, 0, 0.)
      _, _, _, total = lax.while_loop(cond_fun, body_fun, init_val)
      return total

    cfun = api.jit(sum_first_n)
    x = npr.RandomState(0).randn(10)

    for num in [0, 5, 10, 15]:
      self.assertAllClose(sum_first_n(x, num), onp.sum(x[:num]),
                          check_dtypes=False)
      self.assertAllClose(cfun(x, num), onp.sum(x[:num]), check_dtypes=False)
      self.assertAllClose(cfun(x, num), onp.sum(x[:num]), check_dtypes=False)

  def testWhileLoopBatched(self):
    def fun(x):
      return lax.while_loop(lambda x: x < 3, lambda x: x + 2, x)

    ans = api.vmap(fun)(onp.array([0, 1, 2, 3]))
    expected = onp.array([4, 3, 4, 3])
    self.assertAllClose(ans, expected, check_dtypes=False)

    fun = api.jit(fun)
    ans = api.vmap(fun)(onp.array([0, 1, 2, 3]))
    expected = onp.array([4, 3, 4, 3])
    self.assertAllClose(ans, expected, check_dtypes=False)

  def testWhileLoopCondConstsBatched(self):
    def fun(x, y):
      return lax.while_loop(lambda x: x < y, lambda x: x + 2, x)

    ans = api.vmap(fun, in_axes=(None, 0))(0, onp.array([2, 3]))
    expected = onp.array([2, 4])
    self.assertAllClose(ans, expected, check_dtypes=False)

  def testWhileLoopBodyConstsBatched(self):
    def fun(x, y):
      return lax.while_loop(lambda x: x < 3, lambda x: x + y, x)

    ans = api.vmap(fun, in_axes=(None, 0))(0, onp.array([2, 3]))
    expected = onp.array([4, 3])
    self.assertAllClose(ans, expected, check_dtypes=False)

  def testWhileLoopTupleBatched(self):
    def cond_fun(loop_carry):
      x, y = loop_carry
      return x + y < 5

    def body_fun(loop_carry):
      x, y = loop_carry
      x = x + 1
      return x, y

    def fun(x, y):
      return lax.while_loop(cond_fun, body_fun, (x, y))

    ans = api.vmap(fun)(onp.array([0, 0]), onp.array([1, 2]))
    expected = (onp.array([4, 3]), onp.array([1, 2]))
    self.assertAllClose(ans, expected, check_dtypes=False)

  def testForiLoopBatched(self):
    def body_fun(i, loop_carry):
      x, y = loop_carry
      x = x + 1
      y = y + 2
      return x, y

    def fun(x):
      return lax.fori_loop(0, 10, body_fun, (x, 0))

    ans = api.vmap(fun)(onp.array([0, 1]))
    expected = (onp.array([10, 11]), onp.array([20, 20]))
    self.assertAllClose(ans, expected, check_dtypes=False)

  def testForiLoopBasic(self):
    def count(num):
      def body_fun(i, tot):
        return lax.add(tot, i)
      return lax.fori_loop(0, num, body_fun, 0)

    cfun = api.jit(count)

    self.assertEqual(count(2), 1)
    self.assertEqual(count(2), cfun(2))
    self.assertEqual(count(3), 3)
    self.assertEqual(count(3), cfun(3))
    self.assertEqual(count(4), 6)
    self.assertEqual(count(4), cfun(4))

  def testForiLoopClosure(self):
    def count(num):
      def body_fun(i, tot):
        return lax.add(num, lax.add(tot, i))
      return lax.fori_loop(0, num, body_fun, 0)

    cfun = api.jit(count)

    self.assertEqual(count(2), 1 + 2**2)
    self.assertEqual(count(2), cfun(2))
    self.assertEqual(count(3), 3 + 3**2)
    self.assertEqual(count(3), cfun(3))
    self.assertEqual(count(4), 6 + 4**2)
    self.assertEqual(count(4), cfun(4))

  def testForiLoopTupleState(self):
    def sum_first_n(arr, num):
      def body_fun(i, state):
        arr, total = state
        arr_i = lax.dynamic_index_in_dim(arr, i, 0, False)
        return (arr, lax.add(total, arr_i))

      init_val = (arr, 0.)
      _, total = lax.fori_loop(0, lax.min(arr.shape[0], num), body_fun,
                               init_val)
      return total

    cfun = api.jit(sum_first_n)
    x = npr.RandomState(0).randn(10)

    for num in [0, 5, 10, 15]:
      self.assertAllClose(sum_first_n(x, num), onp.sum(x[:num]),
                          check_dtypes=False)
      self.assertAllClose(cfun(x, num), onp.sum(x[:num]), check_dtypes=False)
      self.assertAllClose(cfun(x, num), onp.sum(x[:num]), check_dtypes=False)

  def testForiLoopDictState(self):
    def sum_first_n(arr, num):
      def body_fun(i, state):
        arr, total = state['arr'], state['total']
        arr_i = lax.dynamic_index_in_dim(arr, i, 0, False)
        return {'arr': arr, 'total': lax.add(total, arr_i)}

      init_val = {'arr': arr, 'total': 0.}
      out_val = lax.fori_loop(0, lax.min(arr.shape[0], num), body_fun, init_val)
      return out_val['total']

    cfun = api.jit(sum_first_n)
    x = npr.RandomState(0).randn(10)

    for num in [0, 5, 10, 15]:
      self.assertAllClose(sum_first_n(x, num), onp.sum(x[:num]),
                          check_dtypes=False)
      self.assertAllClose(cfun(x, num), onp.sum(x[:num]), check_dtypes=False)
      self.assertAllClose(cfun(x, num), onp.sum(x[:num]), check_dtypes=False)

  def testForiLoopEmptyTupleInState(self):
    def sum_first_n(arr, num):
      def body_fun(i, state):
        arr, total, _ = state
        arr_i = lax.dynamic_index_in_dim(arr, i, 0, False)
        return (arr, lax.add(total, arr_i), ())

      init_val = (arr, 0., ())
      _, tot, _ = lax.fori_loop(0, lax.min(arr.shape[0], num), body_fun, init_val)
      return tot

    cfun = api.jit(sum_first_n)
    x = npr.RandomState(0).randn(10)

    for num in [0, 5, 10, 15]:
      self.assertAllClose(sum_first_n(x, num), onp.sum(x[:num]),
                          check_dtypes=False)
      self.assertAllClose(cfun(x, num), onp.sum(x[:num]), check_dtypes=False)
      self.assertAllClose(cfun(x, num), onp.sum(x[:num]), check_dtypes=False)

  def testCond(self):
    def fun(x):
      if x < 3:
        return (x, x)
      else:
        y = lax.mul(2, x)
        return y, lax.mul(2, y)

    @api.jit
    def cfun(x):
      def false_fun(x):
        y = lax.mul(2, x)
        return y, lax.mul(2, y)
      return lax.cond(lax.lt(x, 3), x, lambda x: (x, x), x, false_fun)

    self.assertEqual(fun(0), cfun(0))
    self.assertEqual(fun(0), (0, 0))
    self.assertEqual(fun(1), cfun(1))
    self.assertEqual(fun(1), (1, 1))
    self.assertEqual(fun(2), cfun(2))
    self.assertEqual(fun(2), (2, 2))
    self.assertEqual(fun(3), cfun(3))
    self.assertEqual(fun(3), (6, 12))
    self.assertEqual(fun(4), cfun(4))
    self.assertEqual(fun(4), (8, 16))

  def testNestedCond(self):
    def fun(x):
      if x < 2:
        return lax.mul(2, x)
      else:
        if x < 5:
          return lax.mul(3, x)
        else:
          return lax.mul(4, x)

    @api.jit
    def cfun(x):
      return lax.cond(
          lax.lt(x, 2),
          x, lambda x: lax.mul(2, x),
          x, lambda x: lax.cond(lax.lt(x, 5),
                                x, lambda x: lax.mul(3, x),
                                4, lambda y: lax.mul(y, x)))

    self.assertEqual(cfun(1), 2)
    self.assertEqual(cfun(3), 9)
    self.assertEqual(cfun(6), 24)
    self.assertEqual(cfun(1), fun(1))
    self.assertEqual(cfun(3), fun(3))
    self.assertEqual(cfun(6), fun(6))

  def testCondOneBranchConstant(self):
    def fun(x):
      if x < 3:
        return 5.
      else:
        return x

    @api.jit
    def cfun(x):
      return lax.cond(lax.lt(x, 3), x, lambda x: 5, x, lambda x: x)

    self.assertEqual(fun(0), cfun(0))
    self.assertEqual(cfun(0), 5)
    self.assertEqual(fun(4), cfun(4))
    self.assertEqual(cfun(4), 4)

  def testCondOneBranchConstantTuple(self):
    def fun(x):
      if x < 3:
        return (1., 2., 3.)
      else:
        return (x, 2., 4.)

    @api.jit
    def cfun(x):
      return lax.cond(lax.lt(x, 3),
                      x, lambda x: (1, 2., 3.),
                      x, lambda x: (x, 2., 4.))

    self.assertEqual(fun(0), cfun(0))
    self.assertEqual(cfun(0), (1, 2., 3.))
    self.assertEqual(fun(4), cfun(4))
    self.assertEqual(cfun(4), (4, 2., 4.))

  def testCondBatched(self):
    def fun(x, y, z):
      pred = lax.lt(x, 3)
      true_fun = lambda y: y
      false_fun = lambda z: lax.neg(z)
      return lax.cond(pred, y, true_fun, z, false_fun)

    # these cases stay as cond
    x = onp.array(2)
    y = onp.array([1, 2])
    z = onp.array([3, 4])
    ans = api.vmap(fun, (None, 0, 0))(x, y, z)
    jaxpr = api.make_jaxpr(api.vmap(fun, (None, 0, 0)))(x, y, z)
    expected = onp.array([1, 2])
    self.assertAllClose(ans, expected, check_dtypes=False)
    assert "select" not in str(jaxpr)

    x = onp.array(4)
    ans = api.vmap(fun, (None, 0, 0))(x, y, z)
    jaxpr = api.make_jaxpr(api.vmap(fun, (None, 0, 0)))(x, y, z)
    expected = onp.array([-3, -4])
    self.assertAllClose(ans, expected, check_dtypes=False)
    assert "select" not in str(jaxpr)

    fun = api.jit(fun)
    ans = api.vmap(fun, (None, 0, 0))(x, y, z)
    expected = onp.array([-3, -4])
    self.assertAllClose(ans, expected, check_dtypes=False)

    z = onp.array(5)
    ans = api.vmap(fun, (None, 0, None))(x, y, z)
    jaxpr = api.make_jaxpr(api.vmap(fun, (None, 0, None)))(x, y, z)
    expected = onp.array([-5, -5])
    self.assertAllClose(ans, expected, check_dtypes=False)
    assert "select" not in str(jaxpr)


    # these cases become select
    x = onp.array([2, 4])
    ans = api.vmap(fun, (0, 0, None))(x, y, z)
    jaxpr = api.make_jaxpr(api.vmap(fun, (0, 0, None)))(x, y, z)
    expected = onp.array([1, -5])
    self.assertAllClose(ans, expected, check_dtypes=False)
    assert "select" in str(jaxpr)

    z = onp.array([3, 4])
    ans = api.vmap(fun)(x, y, z)
    jaxpr = api.make_jaxpr(api.vmap(fun))(x, y, z)
    expected = onp.array([1, -4])
    self.assertAllClose(ans, expected, check_dtypes=False)
    assert "select" in str(jaxpr)

  def testIssue1263(self):
    def f(rng, x):
      cond = random.bernoulli(rng)
      return lax.cond(cond, x, lambda x: x, np.abs(x) - 1., lambda x: x)

    def body_fn(i, state):
      rng, x = state
      key, subkey = random.split(rng)
      return key, f(subkey, x)

    def g(rng, x):
      return lax.fori_loop(0, 10, body_fn, (rng, x))

    api.vmap(g)(random.split(random.PRNGKey(0), 3), np.ones((3, 4)))

  def testIssue514(self):
    # just check this doesn't crash
    lax.cond(True,
            (0, 0), lambda x: (x[0], 0),
            (1, 1), lambda x: x)

  def testIssue649(self):
    from jax import lax

    def body(x):
      a, b = x
      return (7, b + 1)

    def cond(x):
      a, b = x
      return b < 10

    out = lax.while_loop(cond, body, (33, 4))
    self.assertEqual(out, (7, 10))

  @parameterized.named_parameters(
      {"testcase_name": "_jit_scan={}_jit_f={}".format(jit_scan, jit_f),
       "jit_scan": jit_scan, "jit_f": jit_f}
      for jit_scan in [False, True]
      for jit_f in [False, True])
  def testScanImpl(self, jit_scan, jit_f):
    rng = onp.random.RandomState(0)

    d = rng.randn(2)
    def f(c, a):
      assert a.shape == (3,)
      assert c.shape == (4,)
      b = np.cos(np.sum(np.sin(a)) + np.sum(np.cos(c)) + np.sum(np.tan(d)))
      c = np.sin(c * b)
      assert b.shape == ()
      return c, b

    if jit_f:
      f = api.jit(f)
    if jit_scan:
      scan = api.jit(lax.scan, (0,))
    else:
      scan = lax.scan

    as_ = rng.randn(5, 3)
    c = rng.randn(4)

    ans =                scan(f, c, as_)
    expected = scan_reference(f, c, as_)
    self.assertAllClose(ans, expected, check_dtypes=False)

  @parameterized.named_parameters(
      {"testcase_name": "_jit_scan={}_jit_f={}".format(jit_scan, jit_f),
       "jit_scan": jit_scan, "jit_f": jit_f}
      for jit_scan in [False, True]
      for jit_f in [False, True])
  def testScanJVP(self, jit_scan, jit_f):
    rng = onp.random.RandomState(0)

    d = rng.randn(2)
    def f(c, a):
      assert a.shape == (3,)
      assert c.shape == (4,)
      b = np.cos(np.sum(np.sin(a)) + np.sum(np.cos(c)) + np.sum(np.tan(d)))
      c = np.sin(c * b)
      assert b.shape == ()
      return c, b

    if jit_f:
      f = api.jit(f)
    if jit_scan:
      scan = api.jit(lax.scan, (0,))
    else:
      scan = lax.scan

    as_ = rng.randn(5, 3)
    c = rng.randn(4)

    ans = api.jvp(lambda c, as_:                scan(f, c, as_), (c, as_), (c, as_))
    expected = api.jvp(lambda c, as_: scan_reference(f, c, as_), (c, as_), (c, as_))
    self.assertAllClose(ans, expected, check_dtypes=False)

    jtu.check_grads(partial(scan, f), (c, as_), order=2, modes=["fwd"])

  @parameterized.named_parameters(
      {"testcase_name": "_jit_scan={}_jit_f={}".format(jit_scan, jit_f),
       "jit_scan": jit_scan, "jit_f": jit_f}
      for jit_scan in [False, True]
      for jit_f in [False, True])
  def testScanLinearize(self, jit_scan, jit_f):
    rng = onp.random.RandomState(0)

    d = rng.randn(2)
    def f(c, a):
      assert a.shape == (3,)
      assert c.shape == (4,)
      b = np.cos(np.sum(np.sin(a)) + np.sum(np.cos(c)) + np.sum(np.tan(d)))
      c = np.sin(c * b)
      assert b.shape == ()
      return c, b

    if jit_f:
      f = api.jit(f)
    if jit_scan:
      scan = api.jit(lax.scan, (0,))
    else:
      scan = lax.scan

    as_ = rng.randn(5, 3)
    c = rng.randn(4)

    ans = api.linearize(lambda c, as_:                scan(f, c, as_), c, as_)[1](c, as_)
    expected = api.linearize(lambda c, as_: scan_reference(f, c, as_), c, as_)[1](c, as_)
    self.assertAllClose(ans, expected, check_dtypes=False)

  @parameterized.named_parameters(
      {"testcase_name": "_jit_scan={}_jit_f={}".format(jit_scan, jit_f),
       "jit_scan": jit_scan, "jit_f": jit_f}
      for jit_scan in [False, True]
      for jit_f in [False, True])
  def testScanGrad(self, jit_scan, jit_f):
    rng = onp.random.RandomState(0)

    d = rng.randn(2)
    def f(c, a):
      assert a.shape == (3,)
      assert c.shape == (4,)
      b = np.sum(np.sin(a)) + np.sum(np.sin(c)) + np.sum(np.sin(d))
      c = np.sin(c * b)
      assert b.shape == ()
      return c, b

    if jit_f:
      f = api.jit(f)
    if jit_scan:
      scan = api.jit(lax.scan, (0,))
    else:
      scan = lax.scan

    as_ = rng.randn(5, 3)
    c = rng.randn(4)

    ans = api.grad(lambda c, as_:      list(          scan(f, c, as_))[0].sum())(c, as_)
    expected = api.grad(lambda c, as_: list(scan_reference(f, c, as_))[0].sum())(c, as_)
    self.assertAllClose(ans, expected, check_dtypes=False)

    jtu.check_grads(partial(scan, f), (c, as_), order=2, modes=["rev"],
                    atol=1e-3, rtol=1e-3)

  def testScanRnn(self):
    r = npr.RandomState(0)

    n_in = 4
    n_hid = 2
    n_out = 1
    length = 3

    W_trans = r.randn(n_hid, n_hid + n_in)
    W_out = r.randn(n_out, n_hid + n_in)
    params = W_trans, W_out

    inputs = r.randn(length, n_in)
    targets = r.randn(length, n_out)

    def step(params, state, input):
      W_trans, W_out = params
      stacked = np.concatenate([state, input])
      output = np.tanh(np.dot(W_out, stacked))
      next_state = np.tanh(np.dot(W_trans, stacked))
      return next_state, output

    def rnn(params, inputs):
      init_state = np.zeros(n_hid)
      _, outputs = lax.scan(partial(step, params), init_state, inputs)
      return outputs

    def loss(params, inputs, targets):
      predictions = rnn(params, inputs)
      return np.sum((predictions - targets)**2)

    # evaluation doesn't crash
    loss(params, inputs, targets)

    # jvp evaluation doesn't crash
    api.jvp(lambda params: loss(params, inputs, targets), (params,), (params,))

    # jvp numerical check passes
    jtu.check_grads(loss, (params, inputs, targets), order=2, modes=["fwd"])

    # linearize works
    _, expected = api.jvp(loss, (params, inputs, targets),
                          (params, inputs, targets))
    _, linfun = api.linearize(loss, params, inputs, targets)
    ans = linfun(params, inputs, targets)
    self.assertAllClose(ans, expected, check_dtypes=False)

    # gradient evaluation doesn't crash
    api.grad(loss)(params, inputs, targets)

    # gradient check passes
    jtu.check_grads(loss, (params, inputs, targets), order=2)

    # we can vmap to batch things
    batch_size = 7
    batched_inputs = r.randn(batch_size, length, n_in)
    batched_targets = r.randn(batch_size, length, n_out)
    batched_loss = api.vmap(lambda x, y: loss(params, x, y))
    losses = batched_loss(batched_inputs, batched_targets)
    expected = onp.stack(list(map(lambda x, y: loss(params, x, y),
                                  batched_inputs, batched_targets)))
    self.assertAllClose(losses, expected, check_dtypes=False)

  def testIssue711(self):
    # Tests reverse-mode differentiation through a scan for which the scanned
    # function also involves reverse-mode differentiation.
    # See https://github.com/google/jax/issues/711
    def harmonic_bond(conf, params):
      return np.sum(conf * params)

    def minimize_structure(test_params):
      energy_fn = partial(harmonic_bond, params=test_params)
      grad_fn = api.grad(energy_fn)

      def apply_carry(carry, _):
        i, x = carry
        new_x = x - 0.1 * api.grad(energy_fn)(x)
        new_carry = (i+1, new_x)
        return new_carry, _

      x0 = np.array([1., 2., 3.])
      carry_final, _ = lax.scan(apply_carry, (0, x0), np.zeros((75, 0)))
      _, x_final = carry_final
      return x_final

    initial_params = 0.5
    minimize_structure(initial_params)  # doesn't crash

    def loss(test_params):
      x_final = minimize_structure(test_params)
      return np.sum(np.sin(1.0 - x_final))

    api.grad(loss)(0.25)  # doesn't crash

  def testIssue744(self):
    Point = collections.namedtuple('Point', ['x', 'y'])
    p0 = Point(x=np.array(1), y=np.array(2))

    def plus_one(p, iter_idx):
      return Point(p.x+1, p.y+1), iter_idx

    self.assertRaisesRegexp(
        ValueError,
        'scan got value with no leading axis to scan over.*',
        lambda: lax.scan(plus_one, p0, list(range(5))))

  def testScanHigherOrderDifferentiation(self):
    d = 0.75
    def f(c, a):
      b = np.sin(c * np.sum(np.cos(d * a)))
      c = 0.9 * np.cos(d * np.sum(np.sin(c * a)))
      return c, b

    as_ = np.arange(6.).reshape((3, 2))
    c = 1.

    jtu.check_grads(lambda c, as_: lax.scan(f, c, as_), (c, as_),
                    modes=["rev"], order=2)

  @parameterized.named_parameters(
      {"testcase_name": "_jit_scan={}_jit_f={}_in_axes={}".format(
          jit_scan, jit_f, in_axes),
       "jit_scan": jit_scan, "jit_f": jit_f, "in_axes": in_axes}
      for jit_scan in [False, True]
      for jit_f in [False, True]
      for in_axes in itertools.product([None, 0, 1], [None, 0, 1, 2])
      if in_axes != (None, None))
  def testScanVmap(self, jit_scan, jit_f, in_axes):
    rng = onp.random.RandomState(0)

    d = rng.randn(2)
    def f(c, a):
      assert a.shape == (3,)
      assert c.shape == (4,)
      b = np.cos(np.sum(np.sin(a)) + np.sum(np.cos(c)) + np.sum(np.tan(d)))
      c = np.sin(c * b)
      assert b.shape == ()
      return c, b

    if jit_f:
      f = api.jit(f)
    if jit_scan:
      scan = api.jit(lax.scan, (0,))
    else:
      scan = lax.scan

    as_shape = [5, 3]
    c_shape = [4]

    c_bdim, as_bdim = in_axes
    if c_bdim is not None:
      c_shape.insert(c_bdim, 7)
    if as_bdim is not None:
      as_shape.insert(as_bdim, 7)

    as_ = rng.randn(*as_shape)
    c = rng.randn(*c_shape)

    ans = api.vmap(lambda c, as_:                scan(f, c, as_), in_axes)(c, as_)
    expected = api.vmap(lambda c, as_: scan_reference(f, c, as_), in_axes)(c, as_)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def testScanVmapTuples(self):
    def f(c, a):
      a1, a2 = a
      c1, c2 = c
      b = np.sum(np.cos(a1)) * np.sum(np.tan(c2 * a2))
      c = c1 * np.sin(np.sum(a1 * a2)), c2 * np.cos(np.sum(a1))
      return c, b

    in_axes = (0, (1, 2))

    r = onp.random.RandomState(0)
    as_ = (r.randn(3, 7), r.randn(3, 4, 7))
    c = (r.randn(7, 2), r.randn(7))

    expected_c_out, expected_bs = [], []
    for i in range(7):
      c_out, bs = lax.scan(f, (c[0][i], c[1][i]), (as_[0][:,i], as_[1][:,:,i]))
      expected_c_out.append(c_out)
      expected_bs.append(bs)
    expected_c_out_0, expected_c_out_1 = unzip2(expected_c_out)
    expected_c_out = (np.stack(expected_c_out_0), np.stack(expected_c_out_1))
    expected_bs = np.stack(expected_bs)
    expected = expected_c_out, expected_bs

    ans = api.vmap(lambda c, as_:            lax.scan(f, c, as_), in_axes)(c, as_)
    self.assertAllClose(ans, expected, check_dtypes=False)

  # TODO(mattjj, dougalm): fix this test when skip_checks is False
  def testIssue757(self):
    # code from https://github.com/google/jax/issues/757
    def fn(a):
        return np.cos(a)

    def loop(val):
        iterations = 10
        def apply_carry(x, i):
            return api.grad(fn, argnums=(0,))(x)[0], i

        final_val, _ = lax.scan(
            apply_carry,
            val,
            np.arange(iterations)
        )
        return final_val

    arg = 0.5
    api.jit(api.jacfwd(loop, argnums=(0,)))(arg)  # doesn't crash

  # TODO(mattjj): add a test for "the David Sussillo bug"

  def testIssue804(self):
    num_devices = xla_bridge.device_count()
    f = partial(lax.scan, lambda c, x: (c + lax.psum(x, "i") , c), 0.)
    api.pmap(f, axis_name="i")(np.ones((num_devices, 4)))  # doesn't crash

  def testMap(self):
    f = lambda x: x ** 2
    xs = np.arange(10)
    expected = xs ** 2
    actual = lax.map(f, xs)
    self.assertAllClose(actual, expected, check_dtypes=True)

  def testCaching(self):
    def cond(x):
      assert python_should_be_executing
      return x < 5

    def body(x):
      assert python_should_be_executing
      return x + 2

    python_should_be_executing = True
    lax.while_loop(cond, body, 0)

    python_should_be_executing = False
    lax.while_loop(cond, body, 0)

  def testCaching2(self):
    # This second caching test shows a different kind of caching that we haven't
    # implemented (but could!), namely that Python functions that are distinct
    # objects but are equivalent functions trigger cache hits. This kind of
    # caching could be salient when using lambda functions with control flow:
    #
    #   lax.while_loop(lambda x: x < 5, lambda x: x + 2, 0)
    #   lax.while_loop(lambda x: x < 5, lambda x: x + 2, 0)
    #
    # To get a cache hit on the second line we'd need to form a jaxpr and
    # compare them for equality (including the literals on identity). We could
    # implement that by adding a __hash__/__eq__ to core.Jaxpr and
    # core.TypedJaxpr (see #1221).
    raise SkipTest("not implemented")

    def cond(x):
      assert python_should_be_executing
      return x < 5

    def body(x):
      assert python_should_be_executing
      return x + 2

    python_should_be_executing = True
    lax.while_loop(cond, body, 0)

    def cond(x):
      assert python_should_be_executing
      return x < 5

    def body(x):
      assert python_should_be_executing
      return x + 2

    python_should_be_executing = False
    lax.while_loop(cond, body, 0)

  def testWhileCondConstant(self):
    out = lax.while_loop(lambda _: False, lambda _: (), ())  # doesn't crash
    self.assertEqual(out, ())

  def testIssue1316(self):
    def f(carry, _):
      c, key = carry
      key, _ = random.split(key)
      return (c, key), ()

    key = random.PRNGKey(0)
    api.grad(lambda c: lax.scan(f, (c, key), onp.ones(3))[0][0])(0.)  # doesn't crash

  def testIssue1361(self):
    @api.jit
    def jit_run_scan(x):
      def fun(carry, _):
        x, _ = carry
        return (2 * x, 0.), None
      (x, _), _ = lax.scan(fun, (x, 0.), np.arange(3))
      return x

    api.grad(lambda x: jit_run_scan(x))(0.)  # doesn't crash


if __name__ == '__main__':
  absltest.main()
