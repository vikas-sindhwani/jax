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

"""Utilities for working with tree-like container data structures.

This module provides a small set of utility functions for working with tree-like
data structures, such as nested tuples, lists, and dicts. We call these
structures pytrees. They are trees in that they are defined recursively (any
non-pytree is a pytree, i.e. a leaf, and any pytree of pytrees is a pytree) and
can be operated on recursively (object identity equivalence is not preserved by
mapping operations, and the structures cannot contain reference cycles).

The set of Python types that are considered pytree nodes (e.g. that can be
mapped over, rather than treated as leaves) is extensible. There is a single
module-level registry of types, and class hierarchy is ignored. By registering a
new pytree node type, that type in effect becomes transparent to the utility
functions in this file.

The primary purpose of this module is to enable the interoperability between
user defined data structures and JAX transformations (e.g. `jit`). This is not
meant to be a general purpose tree-like data structure handling library.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
from collections import namedtuple
import itertools as it
from six.moves import reduce

from .lib import pytree

from .util import unzip2, partial, safe_map


def tree_map(f, tree):
  """Map a function over a pytree to produce a new pytree.

  Args:
    f: function to be applied at each leaf.
    tree: a pytree to be mapped over.

  Returns:
    A new pytree with the same structure as `tree` but with the value at each
    leaf given by `f(x)` where `x` is the value at the corresponding leaf in
    `tree`.
  """
  leaves, treedef = pytree.flatten(tree)
  return treedef.unflatten(map(f, leaves))

def tree_multimap(f, tree, *rest):
  """Map a multi-input function over pytree args to produce a new pytree.

  Args:
    f: function that takes `1 + len(rest)` arguments, to be applied at the
      corresponding leaves of the pytrees.
    tree: a pytree to be mapped over, with each leaf providing the first
      positional argument to `f`.
    *rest: a tuple of pytrees, each of which has the same structure as tree or
      or has tree as a prefix.
  Returns:
    A new pytree with the same structure as `tree` but with the value at each
    leaf given by `f(x, *xs)` where `x` is the value at the corresponding leaf
    in `tree` and `xs` is the tuple of values at corresponding nodes in
    `rest`.
  """
  leaves, treedef = pytree.flatten(tree)
  all_leaves = [leaves] + [treedef.flatten_up_to(r) for r in rest]
  return treedef.unflatten(f(*xs) for xs in zip(*all_leaves))

def tree_leaves(tree):
  return pytree.flatten(tree)[0]

def process_pytree(process_node, tree):
  leaves, treedef = pytree.flatten(tree)
  return treedef.walk(process_node, None, leaves), treedef

tree_flatten = pytree.flatten

def build_tree(treedef, xs):
  return treedef.from_iterable_tree(xs)

def treedef_is_leaf(treedef):
  return treedef.num_nodes == 1

def tree_unflatten(treedef, xs):
  return treedef.unflatten(xs)

def tree_transpose(outer_treedef, inner_treedef, pytree_to_transpose):
  flat, treedef = tree_flatten(pytree_to_transpose)
  expected_treedef = outer_treedef.compose(inner_treedef)
  if treedef != expected_treedef:
    raise TypeError("Mismatch\n{}\n != \n{}".format(treedef, expected_treedef))

  inner_size = inner_treedef.num_leaves
  outer_size = outer_treedef.num_leaves
  flat = iter(flat)
  lol = [[next(flat) for _ in range(inner_size)] for __ in range(outer_size)]
  transposed_lol = zip(*lol)
  subtrees = map(partial(tree_unflatten, outer_treedef), transposed_lol)
  return tree_unflatten(inner_treedef, subtrees)

def tree_structure(tree):
  _, treedef = pytree.flatten(tree)
  return treedef

def treedef_tuple(trees):
  return pytree.tuple(list(trees))

def treedef_children(treedef):
    return treedef.children()

register_pytree_node = pytree.register_node



def tree_reduce(f, tree):
  return reduce(f, tree_leaves(tree))


def tree_all(tree):
  return all(tree_leaves(tree))


class Partial(functools.partial):
  """A version of functools.partial that works in pytrees.

  Use it for partial function evaluation in a way that is compatible with JAX's
  transformations, e.g., ``Partial(func, *args, **kwargs)``.

  (You need to explicitly opt-in to this behavior because we didn't want to give
  functools.partial different semantics than normal function closures.)
  """

register_pytree_node(
    Partial,
    lambda partial_: ((partial_.args, partial_.keywords), partial_.func),
    lambda func, xs: Partial(func, *xs[0], **xs[1]),
)
