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

"""Tests for the LAPAX linear algebra module."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from functools import partial
import itertools
import unittest

import numpy as onp
import scipy as osp

from absl.testing import absltest
from absl.testing import parameterized

import jax
import jax.lib
from jax import jit, grad, jvp, vmap
from jax import numpy as np
from jax import scipy as jsp
from jax import test_util as jtu
from jax.lib import xla_bridge
from jax.lib import lapack

from jax.config import config
config.parse_flags_with_absl()
FLAGS = config.FLAGS

T = lambda x: onp.swapaxes(x, -1, -2)


float_types = [onp.float32, onp.float64]
complex_types = [onp.complex64, onp.complex128]

def _skip_if_unsupported_type(dtype):
  dtype = onp.dtype(dtype)
  if (not FLAGS.jax_enable_x64 and
      dtype in (onp.dtype('float64'), onp.dtype('complex128'))):
    raise unittest.SkipTest("--jax_enable_x64 is not set")


numpy_version = tuple(map(int, onp.version.version.split('.')))

class NumpyLinalgTest(jtu.JaxTestCase):

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}".format(jtu.format_shape_dtype_string(shape, dtype)),
       "shape": shape, "dtype": dtype, "rng": rng}
      for shape in [(1, 1), (4, 4), (2, 5, 5), (200, 200), (1000, 0, 0)]
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]))
  def testCholesky(self, shape, dtype, rng):
    _skip_if_unsupported_type(dtype)
    def args_maker():
      factor_shape = shape[:-1] + (2 * shape[-1],)
      a = rng(factor_shape, dtype)
      return [onp.matmul(a, np.conj(T(a)))]

    if np.issubdtype(dtype, np.complexfloating) and (
        len(shape) > 2 or jtu.device_under_test() != "cpu"):
      self.skipTest("Unimplemented case for complex Cholesky decomposition.")

    self._CheckAgainstNumpy(onp.linalg.cholesky, np.linalg.cholesky, args_maker,
                            check_dtypes=True, tol=1e-3)
    self._CompileAndCheck(np.linalg.cholesky, args_maker, check_dtypes=True)

    if onp.finfo(dtype).bits == 64:
      jtu.check_grads(np.linalg.cholesky, args_maker(), order=2)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_n={}".format(jtu.format_shape_dtype_string((n,n), dtype)),
       "n": n, "dtype": dtype, "rng": rng}
      for n in [0, 4, 5, 25]  # TODO(mattjj): complex64 unstable on large sizes?
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]))
  def testDet(self, n, dtype, rng):
    _skip_if_unsupported_type(dtype)
    args_maker = lambda: [rng((n, n), dtype)]

    self._CheckAgainstNumpy(onp.linalg.det, np.linalg.det, args_maker,
                            check_dtypes=True, tol=1e-3)
    self._CompileAndCheck(np.linalg.det, args_maker, check_dtypes=True)

  def testDetOfSingularMatrix(self):
    x = np.array([[-1., 3./2], [2./3, -1.]], dtype=onp.float32)
    self.assertAllClose(onp.float32(0), jsp.linalg.det(x), check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}".format(jtu.format_shape_dtype_string(shape, dtype)),
       "shape": shape, "dtype": dtype, "rng": rng}
      for shape in [(0, 0), (1, 1), (3, 3), (4, 4), (10, 10), (200, 200),
                    (2, 2, 2), (2, 3, 3), (3, 2, 2)]
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]))
  @jtu.skip_on_devices("tpu")
  def testSlogdet(self, shape, dtype, rng):
    _skip_if_unsupported_type(dtype)
    args_maker = lambda: [rng(shape, dtype)]

    self._CheckAgainstNumpy(onp.linalg.slogdet, np.linalg.slogdet, args_maker,
                            check_dtypes=True, tol=1e-3)
    self._CompileAndCheck(np.linalg.slogdet, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}".format(jtu.format_shape_dtype_string(shape, dtype)),
       "shape": shape, "dtype": dtype, "rng": rng}
      for shape in [(1, 1), (4, 4), (5, 5), (2, 7, 7)]
      for dtype in float_types
      for rng in [jtu.rand_default()]))
  @jtu.skip_on_devices("tpu")
  def testSlogdetGrad(self, shape, dtype, rng):
    _skip_if_unsupported_type(dtype)
    a = rng(shape, dtype)
    jtu.check_grads(np.linalg.slogdet, (a,), 2, atol=1e-1, rtol=1e-1)

  def testIssue1213(self):
    for n in range(5):
      mat = np.array([onp.diag(onp.ones([5], dtype=onp.float32))*(-.01)] * 2)
      args_maker = lambda: [mat]
      self._CheckAgainstNumpy(onp.linalg.slogdet, np.linalg.slogdet, args_maker,
                              check_dtypes=True, tol=1e-3)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}".format(
           jtu.format_shape_dtype_string(shape, dtype)),
       "shape": shape, "dtype": dtype, "rng": rng}
      for shape in [(0, 0), (4, 4), (5, 5), (50, 50), (2, 6, 6)]
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]))
  # TODO(phawkins): enable when there is an eigendecomposition implementation
  # for GPU/TPU.
  @jtu.skip_on_devices("gpu", "tpu")
  def testEig(self, shape, dtype, rng):
    _skip_if_unsupported_type(dtype)
    n = shape[-1]
    args_maker = lambda: [rng(shape, dtype)]

    # Norm, adjusted for dimension and type.
    def norm(x):
      norm = onp.linalg.norm(x, axis=(-2, -1))
      return norm / ((n + 1) * onp.finfo(dtype).eps)

    a, = args_maker()
    w, v = np.linalg.eig(a)
    self.assertTrue(onp.all(norm(onp.matmul(a, v) - w[..., None, :] * v) < 100))

    self._CompileAndCheck(partial(np.linalg.eig), args_maker,
                          check_dtypes=True, rtol=1e-3)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}".format(jtu.format_shape_dtype_string(shape, dtype)),
       "shape": shape, "dtype": dtype, "rng": rng}
      for shape in [(1, 1), (4, 4), (5, 5)]
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]))
  @jtu.skip_on_devices("gpu", "tpu")
  def testEigBatching(self, shape, dtype, rng):
    _skip_if_unsupported_type(dtype)
    shape = (10,) + shape
    args = rng(shape, dtype)
    ws, vs = vmap(np.linalg.eig)(args)
    self.assertTrue(onp.all(onp.linalg.norm(
        onp.matmul(args, vs) - ws[..., None, :] * vs) < 1e-3))

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_n={}_lower={}".format(
           jtu.format_shape_dtype_string((n,n), dtype), lower),
       "n": n, "dtype": dtype, "lower": lower, "rng": rng}
      for n in [0, 4, 5, 50]
      for dtype in float_types + complex_types
      for lower in [False, True]
      for rng in [jtu.rand_default()]))
  # TODO(phawkins): enable when there is an eigendecomposition implementation
  # for TPU.
  @jtu.skip_on_devices("tpu")
  def testEigh(self, n, dtype, lower, rng):
    _skip_if_unsupported_type(dtype)
    args_maker = lambda: [rng((n, n), dtype)]

    uplo = "L" if lower else "U"

    # Norm, adjusted for dimension and type.
    def norm(x):
      norm = onp.linalg.norm(x, axis=(-2, -1))
      return norm / ((n + 1) * onp.finfo(dtype).eps)

    a, = args_maker()
    a = (a + onp.conj(a.T)) / 2
    w, v = np.linalg.eigh(onp.tril(a) if lower else onp.triu(a),
                          UPLO=uplo, symmetrize_input=False)
    self.assertTrue(norm(onp.eye(n) - onp.matmul(onp.conj(T(v)), v)) < 5)
    self.assertTrue(norm(onp.matmul(a, v) - w * v) < 30)

    self._CompileAndCheck(partial(np.linalg.eigh, UPLO=uplo), args_maker,
                          check_dtypes=True, rtol=1e-3)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}_lower={}".format(jtu.format_shape_dtype_string(shape, dtype),
                                   lower),
       "shape": shape, "dtype": dtype, "rng": rng, "lower":lower}
      for shape in [(1, 1), (4, 4), (5, 5), (50, 50)]
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]
      for lower in [True, False]))
  # TODO(phawkins): enable when there is an eigendecomposition implementation
  # for TPU.
  @jtu.skip_on_devices("tpu")
  def testEighGrad(self, shape, dtype, rng, lower):
    self.skipTest("Test fails with numeric errors.")
    uplo = "L" if lower else "U"
    a = rng(shape, dtype)
    a = (a + onp.conj(a.T)) / 2
    a = onp.tril(a) if lower else onp.triu(a)
    # Gradient checks will fail without symmetrization as the eigh jvp rule
    # is only correct for tangents in the symmetric subspace, whereas the
    # checker checks against unconstrained (co)tangents.
    if dtype not in complex_types:
      f = partial(np.linalg.eigh, UPLO=uplo, symmetrize_input=True)
    else:  # only check eigenvalue grads for complex matrices
      f = lambda a: partial(np.linalg.eigh, UPLO=uplo, symmetrize_input=True)(a)[0]
    jtu.check_grads(f, (a,), 2, rtol=1e-1)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}_lower={}".format(jtu.format_shape_dtype_string(shape, dtype),
                                   lower),
       "shape": shape, "dtype": dtype, "rng": rng, "lower":lower, "eps":eps}
      for shape in [(1, 1), (4, 4), (5, 5), (50, 50)]
      for dtype in complex_types
      for rng in [jtu.rand_default()]
      for lower in [True, False]
      for eps in [1e-4]))
  # TODO(phawkins): enable when there is an eigendecomposition implementation
  # for TPU.
  @jtu.skip_on_devices("tpu")
  def testEighGradVectorComplex(self, shape, dtype, rng, lower, eps):
    _skip_if_unsupported_type(dtype)
    # Special case to test for complex eigenvector grad correctness.
    # Exact eigenvector coordinate gradients are hard to test numerically for complex
    # eigensystem solvers given the extra degrees of per-eigenvector phase freedom.
    # Instead, we numerically verify the eigensystem properties on the perturbed
    # eigenvectors.  You only ever want to optimize eigenvector directions, not coordinates!
    uplo = "L" if lower else "U"
    a = rng(shape, dtype)
    a = (a + onp.conj(a.T)) / 2
    a = onp.tril(a) if lower else onp.triu(a)
    a_dot = eps * rng(shape, dtype)
    a_dot = (a_dot + onp.conj(a_dot.T)) / 2
    a_dot = onp.tril(a_dot) if lower else onp.triu(a_dot)
    # evaluate eigenvector gradient and groundtruth eigensystem for perturbed input matrix
    f = partial(np.linalg.eigh, UPLO=uplo)
    (w, v), (dw, dv) = jvp(f, primals=(a,), tangents=(a_dot,))
    new_a = a + a_dot
    new_w, new_v = f(new_a)
    new_a = (new_a + onp.conj(new_a.T)) / 2
    # Assert rtol eigenvalue delta between perturbed eigenvectors vs new true eigenvalues.
    RTOL=1e-2
    assert onp.max(
      onp.abs((onp.diag(onp.dot(onp.conj((v+dv).T), onp.dot(new_a,(v+dv)))) - new_w) / new_w)) < RTOL
    # Redundant to above, but also assert rtol for eigenvector property with new true eigenvalues.
    assert onp.max(
      onp.linalg.norm(onp.abs(new_w*(v+dv) - onp.dot(new_a, (v+dv))), axis=0) /
      onp.linalg.norm(onp.abs(new_w*(v+dv)), axis=0)
    ) < RTOL

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}".format(jtu.format_shape_dtype_string(shape, dtype)),
       "shape": shape, "dtype": dtype, "rng": rng}
      for shape in [(1, 1), (4, 4), (5, 5)]
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]))
  @jtu.skip_on_devices("tpu")
  def testEighBatching(self, shape, dtype, rng):
    _skip_if_unsupported_type(dtype)
    shape = (10,) + shape
    args = rng(shape, dtype)
    args = (args + onp.conj(T(args))) / 2
    ws, vs = vmap(jsp.linalg.eigh)(args)
    self.assertTrue(onp.all(onp.linalg.norm(
        onp.matmul(args, vs) - ws[..., None, :] * vs) < 1e-3))

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_ord={}_axis={}_keepdims={}".format(
         jtu.format_shape_dtype_string(shape, dtype), ord, axis, keepdims),
       "shape": shape, "dtype": dtype, "axis": axis, "keepdims": keepdims,
       "ord": ord, "rng": rng}
      for axis, shape in [
        (None, (1,)), (None, (7,)), (None, (5, 8)),
        (0, (9,)), (0, (4, 5)), ((1,), (10, 7, 3)), ((-2,), (4, 8)),
        (-1, (6, 3)), ((0, 2), (3, 4, 5)), ((2, 0), (7, 8, 9)),
        (None, (7, 8, 11))]
      for keepdims in [False, True]
      for ord in (
          [None] if axis is None and len(shape) > 2
          else [None, 0, 1, 2, 3, -1, -2, -3, np.inf, -np.inf]
          if (axis is None and len(shape) == 1) or
             isinstance(axis, int) or
             (isinstance(axis, tuple) and len(axis) == 1)
          else [None, 'fro', 1, 2, -1, -2, np.inf, -np.inf, 'nuc'])
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]))
  def testNorm(self, shape, dtype, ord, axis, keepdims, rng):
    _skip_if_unsupported_type(dtype)
    if (ord in ('nuc', 2, -2) and (
        jtu.device_under_test() != "cpu" or
        (isinstance(axis, tuple) and len(axis) == 2))):
      raise unittest.SkipTest("No adequate SVD implementation available")

    args_maker = lambda: [rng(shape, dtype)]
    onp_fn = partial(onp.linalg.norm, ord=ord, axis=axis, keepdims=keepdims)
    np_fn = partial(np.linalg.norm, ord=ord, axis=axis, keepdims=keepdims)
    # Older numpy versions promote to float64 unnecessarily..
    check_dtypes = numpy_version >= (1, 15)
    self._CheckAgainstNumpy(onp_fn, np_fn, args_maker,
                            check_dtypes=check_dtypes, tol=1e-3)
    self._CompileAndCheck(np_fn, args_maker, check_dtypes=check_dtypes)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_n={}_full_matrices={}_compute_uv={}".format(
          jtu.format_shape_dtype_string(b + (m, n), dtype), full_matrices,
          compute_uv),
       "b": b, "m": m, "n": n, "dtype": dtype, "full_matrices": full_matrices,
       "compute_uv": compute_uv, "rng": rng}
      for b in [(), (3,), (2, 3)]
      for m in [2, 7, 29, 53]
      for n in [2, 7, 29, 53]
      for dtype in float_types + complex_types
      for full_matrices in [False, True]
      for compute_uv in [False, True]
      for rng in [jtu.rand_default()]))
  @jtu.skip_on_devices("tpu")
  def testSVD(self, b, m, n, dtype, full_matrices, compute_uv, rng):
    _skip_if_unsupported_type(dtype)
    if b != () and jax.lib.version <= (0, 1, 28):
      raise unittest.SkipTest("Batched SVD requires jaxlib 0.1.29")
    args_maker = lambda: [rng(b + (m, n), dtype)]

    # Norm, adjusted for dimension and type.
    def norm(x):
      norm = onp.linalg.norm(x, axis=(-2, -1))
      return norm / (max(m, n) * onp.finfo(dtype).eps)

    a, = args_maker()
    out = np.linalg.svd(a, full_matrices=full_matrices, compute_uv=compute_uv)
    if compute_uv:
      # Check the reconstructed matrices
      if full_matrices:
        k = min(m, n)
        if m < n:
          self.assertTrue(onp.all(
              norm(a - onp.matmul(out[1][..., None, :] * out[0], out[2][..., :k, :])) < 50))
        else:
          self.assertTrue(onp.all(
              norm(a - onp.matmul(out[1][..., None, :] * out[0][..., :, :k], out[2])) < 350))
      else:
        self.assertTrue(onp.all(
          norm(a - onp.matmul(out[1][..., None, :] * out[0], out[2])) < 300))

      # Check the unitary properties of the singular vector matrices.
      self.assertTrue(onp.all(norm(onp.eye(out[0].shape[-1]) - onp.matmul(onp.conj(T(out[0])), out[0])) < 10))
      if m >= n:
        self.assertTrue(onp.all(norm(onp.eye(out[2].shape[-1]) - onp.matmul(onp.conj(T(out[2])), out[2])) < 10))
      else:
        self.assertTrue(onp.all(norm(onp.eye(out[2].shape[-2]) - onp.matmul(out[2], onp.conj(T(out[2])))) < 20))

    else:
      self.assertTrue(onp.allclose(onp.linalg.svd(a, compute_uv=False), onp.asarray(out), atol=1e-4, rtol=1e-4))

    self._CompileAndCheck(partial(np.linalg.svd, full_matrices=full_matrices, compute_uv=compute_uv),
                          args_maker, check_dtypes=True)
    if not full_matrices:
      svd = partial(np.linalg.svd, full_matrices=False)
      jtu.check_jvp(svd, partial(jvp, svd), (a,), rtol=1e-2, atol=1e-1)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}_fullmatrices={}".format(
          jtu.format_shape_dtype_string(shape, dtype), full_matrices),
       "shape": shape, "dtype": dtype, "full_matrices": full_matrices,
       "rng": rng}
      for shape in [(1, 1), (3, 3), (3, 4), (2, 10, 5), (2, 200, 100)]
      for dtype in float_types + complex_types
      for full_matrices in [False, True]
      for rng in [jtu.rand_default()]))
  def testQr(self, shape, dtype, full_matrices, rng):
    _skip_if_unsupported_type(dtype)
    if (onp.issubdtype(dtype, onp.complexfloating) and
        (jtu.device_under_test() == "tpu" or jax.lib.version <= (0, 1, 27))):
      raise unittest.SkipTest("No complex QR implementation")
    m, n = shape[-2:]

    if full_matrices:
      mode, k = "complete", m
    else:
      mode, k = "reduced", min(m, n)

    a = rng(shape, dtype)
    lq, lr = np.linalg.qr(a, mode=mode)

    # onp.linalg.qr doesn't support batch dimensions. But it seems like an
    # inevitable extension so we support it in our version.
    nq = onp.zeros(shape[:-2] + (m, k), dtype)
    nr = onp.zeros(shape[:-2] + (k, n), dtype)
    for index in onp.ndindex(*shape[:-2]):
      nq[index], nr[index] = onp.linalg.qr(a[index], mode=mode)

    max_rank = max(m, n)

    # Norm, adjusted for dimension and type.
    def norm(x):
      n = onp.linalg.norm(x, axis=(-2, -1))
      return n / (max_rank * onp.finfo(dtype).eps)

    def compare_orthogonal(q1, q2):
      # Q is unique up to sign, so normalize the sign first.
      sum_of_ratios = onp.sum(onp.divide(q1, q2), axis=-2, keepdims=True)
      phases = onp.divide(sum_of_ratios, onp.abs(sum_of_ratios))
      q1 *= phases
      self.assertTrue(onp.all(norm(q1 - q2) < 30))

    # Check a ~= qr
    self.assertTrue(onp.all(norm(a - onp.matmul(lq, lr)) < 30))

    # Compare the first 'k' vectors of Q; the remainder form an arbitrary
    # orthonormal basis for the null space.
    compare_orthogonal(nq[..., :k], lq[..., :k])

    # Check that q is close to unitary.
    self.assertTrue(onp.all(
        norm(onp.eye(k) -onp.matmul(onp.conj(T(lq)), lq)) < 5))

    if not full_matrices and m >= n:
        jtu.check_jvp(np.linalg.qr, partial(jvp, np.linalg.qr), (a,), atol=1e-3)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_shape={}".format(
          jtu.format_shape_dtype_string(shape, dtype)),
       "shape": shape, "dtype": dtype,
       "rng": rng}
      for shape in [(10, 4, 5), (5, 3, 3), (7, 6, 4)]
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]))
  def testQrBatching(self, shape, dtype, rng):
    args = rng(shape, np.float32)
    qs, rs = vmap(jsp.linalg.qr)(args)
    self.assertTrue(onp.all(onp.linalg.norm(args - onp.matmul(qs, rs)) < 1e-3))

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs={}_rhs={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype)),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "rng": rng}
      for lhs_shape, rhs_shape in [
          ((1, 1), (1, 1)),
          ((4, 4), (4,)),
          ((8, 8), (8, 4)),
          ((1, 2, 2), (3, 2)),
          ((2, 1, 3, 3), (2, 4, 3, 4)),
      ]
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]))
  def testSolve(self, lhs_shape, rhs_shape, dtype, rng):
    _skip_if_unsupported_type(dtype)
    args_maker = lambda: [rng(lhs_shape, dtype), rng(rhs_shape, dtype)]

    self._CheckAgainstNumpy(onp.linalg.solve, np.linalg.solve, args_maker,
                            check_dtypes=True, tol=1e-3)
    self._CompileAndCheck(np.linalg.solve, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}".format(jtu.format_shape_dtype_string(shape, dtype)),
       "shape": shape, "dtype": dtype, "rng": rng}
      for shape in [(1, 1), (4, 4), (2, 5, 5), (200, 200), (5, 5, 5)]
      for dtype in float_types
      for rng in [jtu.rand_default()]))
  def testInv(self, shape, dtype, rng):
    _skip_if_unsupported_type(dtype)
    if jtu.device_under_test() == "gpu" and shape == (200, 200):
      raise unittest.SkipTest("Test is flaky on GPU")

    def args_maker():
      invertible = False
      while not invertible:
        a = rng(shape, dtype)
        try:
          onp.linalg.inv(a)
          invertible = True
        except onp.linalg.LinAlgError:
          pass
      return [a]

    self._CheckAgainstNumpy(onp.linalg.inv, np.linalg.inv, args_maker,
                            check_dtypes=True, tol=1e-3)
    self._CompileAndCheck(np.linalg.inv, args_maker, check_dtypes=True)

  # Regression test for incorrect type for eigenvalues of a complex matrix.
  @jtu.skip_on_devices("tpu")  # TODO(phawkins): No eigh implementation on TPU.
  def testIssue669(self):
    def test(x):
      val, vec = np.linalg.eigh(x)
      return np.real(np.sum(val))

    grad_test_jc = jit(grad(jit(test)))
    xc = onp.eye(3, dtype=onp.complex)
    self.assertAllClose(xc, grad_test_jc(xc), check_dtypes=True)

  def testIssue1151(self):
    A = np.array(onp.random.randn(100, 3, 3), dtype=np.float32)
    b = np.array(onp.random.randn(100, 3), dtype=np.float32)
    x = np.linalg.solve(A, b)
    self.assertAllClose(vmap(np.dot)(A, x), b, atol=1e-3, rtol=1e-3,
                        check_dtypes=True)
    jac0 = jax.jacobian(np.linalg.solve, argnums=0)(A, b)
    jac1 = jax.jacobian(np.linalg.solve, argnums=1)(A, b)
    jac0 = jax.jacobian(np.linalg.solve, argnums=0)(A[0], b[0])
    jac1 = jax.jacobian(np.linalg.solve, argnums=1)(A[0], b[0])

  @jtu.skip_on_devices("tpu")  # TODO(phawkins): No eigh implementation on TPU.
  def testIssue1383(self):
    seed = jax.random.PRNGKey(0)
    tmp = jax.random.uniform(seed, (2,2))
    a = np.dot(tmp, tmp.T)

    def f(inp):
      val, vec = np.linalg.eigh(inp)
      return np.dot(np.dot(vec, inp), vec.T)

    grad_func = jax.jacfwd(f)
    hess_func = jax.jacfwd(grad_func)
    cube_func = jax.jacfwd(hess_func)
    self.assertFalse(onp.any(onp.isnan(cube_func(a))))


class ScipyLinalgTest(jtu.JaxTestCase):

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}".format(jtu.format_shape_dtype_string(shape, dtype)),
       "shape": shape, "dtype": dtype, "rng": rng}
      for shape in [(1, 1), (4, 5), (10, 5), (50, 50)]
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]))
  def testLu(self, shape, dtype, rng):
    _skip_if_unsupported_type(dtype)
    args_maker = lambda: [rng(shape, dtype)]
    x, = args_maker()
    p, l, u = jsp.linalg.lu(x)
    self.assertAllClose(x, onp.matmul(p, onp.matmul(l, u)), check_dtypes=True)
    self._CompileAndCheck(jsp.linalg.lu, args_maker, check_dtypes=True)

  def testLuOfSingularMatrix(self):
    x = np.array([[-1., 3./2], [2./3, -1.]], dtype=onp.float32)
    p, l, u = jsp.linalg.lu(x)
    self.assertAllClose(x, onp.matmul(p, onp.matmul(l, u)), check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}".format(jtu.format_shape_dtype_string(shape, dtype)),
       "shape": shape, "dtype": dtype, "rng": rng}
      for shape in [(1, 1), (4, 5), (10, 5), (10, 10), (6, 7, 7)]
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]))
  @jtu.skip_on_devices("tpu")  # TODO(phawkins): precision problems on TPU.
  def testLuGrad(self, shape, dtype, rng):
    _skip_if_unsupported_type(dtype)
    a = rng(shape, dtype)
    lu = vmap(jsp.linalg.lu) if len(shape) > 2 else jsp.linalg.lu
    jtu.check_grads(lu, (a,), 2, atol=5e-2, rtol=1e-1)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_shape={}".format(jtu.format_shape_dtype_string(shape, dtype)),
       "shape": shape, "dtype": dtype, "rng": rng}
      for shape in [(4, 5), (6, 5)]
      for dtype in [np.float32]
      for rng in [jtu.rand_default()]))
  def testLuBatching(self, shape, dtype, rng):
    _skip_if_unsupported_type(dtype)
    args = [rng(shape, np.float32) for _ in range(10)]
    expected = list(osp.linalg.lu(x) for x in args)
    ps = onp.stack([out[0] for out in expected])
    ls = onp.stack([out[1] for out in expected])
    us = onp.stack([out[2] for out in expected])

    actual_ps, actual_ls, actual_us = vmap(jsp.linalg.lu)(np.stack(args))
    self.assertAllClose(ps, actual_ps, check_dtypes=True)
    self.assertAllClose(ls, actual_ls, check_dtypes=True)
    self.assertAllClose(us, actual_us, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_n={}".format(jtu.format_shape_dtype_string((n,n), dtype)),
       "n": n, "dtype": dtype, "rng": rng}
      for n in [1, 4, 5, 200]
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]))
  def testLuFactor(self, n, dtype, rng):
    _skip_if_unsupported_type(dtype)
    args_maker = lambda: [rng((n, n), dtype)]

    x, = args_maker()
    lu, piv = jsp.linalg.lu_factor(x)
    l = onp.tril(lu, -1) + onp.eye(n, dtype=dtype)
    u = onp.triu(lu)
    for i in range(n):
      x[[i, piv[i]],] = x[[piv[i], i],]
    self.assertAllClose(x, onp.matmul(l, u), check_dtypes=True, rtol=1e-3)
    self._CompileAndCheck(jsp.linalg.lu_factor, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs={}_rhs={}_trans={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype),
           trans),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "trans": trans, "rng": rng}
      for lhs_shape, rhs_shape in [
          ((1, 1), (1, 1)),
          ((4, 4), (4,)),
          ((8, 8), (8, 4, 2)),
      ]
      for trans in [0, 1, 2]
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]))
  def testLuSolve(self, lhs_shape, rhs_shape, dtype, trans, rng):
    _skip_if_unsupported_type(dtype)
    osp_fun = lambda lu, piv, rhs: osp.linalg.lu_solve((lu, piv), rhs, trans=trans)
    jsp_fun = lambda lu, piv, rhs: jsp.linalg.lu_solve((lu, piv), rhs, trans=trans)

    def args_maker():
      a = rng(lhs_shape, dtype)
      lu, piv = osp.linalg.lu_factor(a)
      return [lu, piv, rng(rhs_shape, dtype)]

    self._CheckAgainstNumpy(osp_fun, jsp_fun, args_maker,
                            check_dtypes=True, tol=1e-3)
    self._CompileAndCheck(jsp_fun, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs={}_rhs={}_sym_pos={}_lower={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype),
           sym_pos, lower),
       "lhs_shape": lhs_shape, "rhs_shape": rhs_shape, "dtype": dtype,
       "sym_pos": sym_pos, "lower": lower, "rng": rng}
      for lhs_shape, rhs_shape in [
          ((1, 1), (1, 1)),
          ((4, 4), (4,)),
          ((8, 8), (8, 4)),
      ]
      for sym_pos, lower in [
        (False, False),
        (True, False),
        (True, True),
      ]
      for dtype in float_types + complex_types
      for rng in [jtu.rand_default()]))
  def testSolve(self, lhs_shape, rhs_shape, dtype, sym_pos, lower, rng):
    _skip_if_unsupported_type(dtype)
    if (sym_pos and onp.issubdtype(dtype, onp.complexfloating) and
        jtu.device_under_test() == "tpu"):
      raise unittest.SkipTest(
        "Complex Cholesky decomposition not implemented on TPU")
    osp_fun = lambda lhs, rhs: osp.linalg.solve(lhs, rhs, sym_pos=sym_pos, lower=lower)
    jsp_fun = lambda lhs, rhs: jsp.linalg.solve(lhs, rhs, sym_pos=sym_pos, lower=lower)

    def args_maker():
      a = rng(lhs_shape, dtype)
      if sym_pos:
        a = onp.matmul(a, onp.conj(T(a)))
        a = onp.tril(a) if lower else onp.triu(a)
      return [a, rng(rhs_shape, dtype)]

    self._CheckAgainstNumpy(osp_fun, jsp_fun, args_maker,
                            check_dtypes=True, tol=1e-3)
    self._CompileAndCheck(jsp_fun, args_maker, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs={}_rhs={}_lower={}_transposea={}_unit_diagonal={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype),
           lower, transpose_a, unit_diagonal),
       "lower": lower, "transpose_a": transpose_a,
       "unit_diagonal": unit_diagonal, "lhs_shape": lhs_shape,
       "rhs_shape": rhs_shape, "dtype": dtype, "rng": rng}
      for lower in [False, True]
      for transpose_a in [False, True]
      for unit_diagonal in [False, True]
      for lhs_shape, rhs_shape in [
          ((4, 4), (4,)),
          ((4, 4), (4, 3)),
          ((2, 8, 8), (2, 8, 10)),
      ]
      for dtype in float_types
      for rng in [jtu.rand_default()]))
  def testSolveTriangular(self, lower, transpose_a, unit_diagonal, lhs_shape,
                          rhs_shape, dtype, rng):
    _skip_if_unsupported_type(dtype)
    k = rng(lhs_shape, dtype)
    l = onp.linalg.cholesky(onp.matmul(k, T(k))
                            + lhs_shape[-1] * onp.eye(lhs_shape[-1]))
    l = l.astype(k.dtype)
    b = rng(rhs_shape, dtype)

    if unit_diagonal:
      a = onp.tril(l, -1) + onp.eye(lhs_shape[-1], dtype=dtype)
    else:
      a = l
    a = a if lower else T(a)

    inv = onp.linalg.inv(T(a) if transpose_a else a).astype(a.dtype)
    if len(lhs_shape) == len(rhs_shape):
      onp_ans = onp.matmul(inv, b)
    else:
      onp_ans = onp.einsum("...ij,...j->...i", inv, b)

    # The standard scipy.linalg.solve_triangular doesn't support broadcasting.
    # But it seems like an inevitable extension so we support it.
    ans = jsp.linalg.solve_triangular(
        l if lower else T(l), b, trans=1 if transpose_a else 0, lower=lower,
        unit_diagonal=unit_diagonal)

    self.assertAllClose(onp_ans, ans, check_dtypes=True)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name":
       "_lhs={}_rhs={}_lower={}_transposea={}_unit_diagonal={}".format(
           jtu.format_shape_dtype_string(lhs_shape, dtype),
           jtu.format_shape_dtype_string(rhs_shape, dtype),
           lower, transpose_a, unit_diagonal),
       "lower": lower, "transpose_a": transpose_a,
       "unit_diagonal": unit_diagonal, "lhs_shape": lhs_shape,
       "rhs_shape": rhs_shape, "dtype": dtype, "rng": rng}
      for lower in [False, True]
      for unit_diagonal in [False, True]
      for dtype in float_types + complex_types
      for transpose_a in (
        [0, 1] if onp.issubdtype(dtype, np.floating) else [0, 1, 2])
      for lhs_shape, rhs_shape in [
          ((4, 4), (4,)),
          ((4, 4), (4, 3)),
          ((2, 8, 8), (2, 8, 10)),
      ]
      for rng in [jtu.rand_default()]))
  @jtu.skip_on_devices("tpu")  # TODO(phawkins): Test fails on TPU.
  def testSolveTriangularGrad(self, lower, transpose_a, unit_diagonal,
                              lhs_shape, rhs_shape, dtype, rng):
    _skip_if_unsupported_type(dtype)
    A = np.tril(rng(lhs_shape, dtype) + 5 * onp.eye(lhs_shape[-1], dtype=dtype))
    A = A if lower else T(A)
    B = rng(rhs_shape, dtype)
    f = partial(jsp.linalg.solve_triangular, lower=lower, trans=transpose_a,
                unit_diagonal=unit_diagonal)
    jtu.check_grads(f, (A, B), 2, rtol=2e-2, eps=1e-3)


if __name__ == "__main__":
  absltest.main()
