import math
import heapq
import random
import functools
import itertools
import collections

from opt_einsum.paths import (
    ssa_greedy_optimize,
    register_path_fn,
    ssa_to_linear,
)
from opt_einsum.path_random import thermal_chooser

from .core import (
    jitter_dict,
    ContractionTree,
    get_hypergraph,
)
from .utils import oset
from .hyper import register_hyper_function

# ------------------------------ GREEDY HYPER ------------------------------- #


def cost_memory_removed_mod(
    size12, size1, size2, k12, k1, k2,
    costmod=1, usesizes=True,
):
    """The default heuristic cost, corresponding to the total reduction in
    memory of performing a contraction.
    """
    if usesizes:
        return size12 - costmod * (size1 + size2)
    return len(k12) - costmod * max(len(k1), len(k2))


def trial_greedy(inputs, output, size_dict,
                 random_strength=0.1,
                 temperature=1.0,
                 rel_temperature=True,
                 costmod=1,
                 usesizes=True):

    rand_size_dict = jitter_dict(size_dict, random_strength)

    cost_fn = functools.partial(cost_memory_removed_mod,
                                costmod=costmod, usesizes=usesizes)
    choose_fn = functools.partial(thermal_chooser, temperature=temperature,
                                  rel_temperature=rel_temperature)

    ssa_path = ssa_greedy_optimize(inputs, output, rand_size_dict,
                                   choose_fn=choose_fn, cost_fn=cost_fn)

    return ContractionTree.from_path(inputs, output, size_dict,
                                     ssa_path=ssa_path)


register_hyper_function(
    name='greedy',
    ssa_func=trial_greedy,
    space={
        'random_strength': {'type': 'FLOAT_EXP', 'min': 0.01, 'max': 10.},
        'temperature': {'type': 'FLOAT_EXP', 'min': 0.01, 'max': 10.},
        'rel_temperature': {'type': 'BOOL'},
        'costmod': {'type': 'FLOAT', 'min': 0.0, 'max': 2.0},
        'usesizes': {'type': 'BOOL'},
    },
)


# --------------------------------------------------------------------------- #


def greconf_rf(inputs, output, size_dict, memory_limit=None):
    """Greedy-reconf path -- find a single greedy path then perform a round of
    cheap subtree reconfigurations to optimize it.
    """
    ssa_path = ssa_greedy_optimize(inputs, output, size_dict)
    tree = ContractionTree.from_path(
        inputs, output, size_dict, ssa_path=ssa_path)
    tree.subtree_reconfigure_(subtree_size=6, minimize='flops')
    return tree.path()


register_path_fn('greedy-rf', greconf_rf)


def greconf_rw(inputs, output, size_dict, memory_limit=None):
    """Greedy-reconf path -- find a single greedy path then perform a round of
    cheap subtree reconfigurations to optimize it.
    """
    ssa_path = ssa_greedy_optimize(inputs, output, size_dict)
    tree = ContractionTree.from_path(
        inputs, output, size_dict, ssa_path=ssa_path)
    tree.subtree_reconfigure_(subtree_size=6, minimize='write')
    return tree.path()


register_path_fn('greedy-rw', greconf_rw)


def greconf_rc(inputs, output, size_dict, memory_limit=None):
    """Greedy-reconf path -- find a single greedy path then perform a round of
    cheap subtree reconfigurations to optimize it.
    """
    ssa_path = ssa_greedy_optimize(inputs, output, size_dict)
    tree = ContractionTree.from_path(
        inputs, output, size_dict, ssa_path=ssa_path)
    tree.subtree_reconfigure_(subtree_size=6, minimize='combo')
    return tree.path()


register_path_fn('greedy-rc', greconf_rc)


# --------------------------------------------------------------------------- #


def _binary_combine(func, x, y):
    if func == 'sum':
        return x + y
    if func == 'mean':
        return (x + y) / 2
    if func == 'max':
        return max(x, y)
    if func == 'min':
        return min(x, y)
    if func == 'diff':
        return abs(x - y)


def gumbel():
    return -math.log(-math.log(random.uniform(0.0, 1.0)))


try:
    import numba as nb
    gumbel = nb.njit(gumbel)
except ImportError:
    pass


class GreedyCompressed:
    """A greedy contraction path finder that takes into account the effect of
    compression, and can also make use of subgraph size and centrality.

    Parameters
    ----------
    chi : int
        The maximum bond size between nodes to compress to.
    coeff_size_compressed : float, optional
        When assessing contractions, how to weight the size of the output
        tensor, post compression.
    coeff_size : float, optional
        When assessing contractions, how to weight the size of the output
        tenor, pre compression.
    coeff_size_inputs : float, optional
        When assessing contractions, how to weight the maximum size of the
        inputs tensors.
    score_size_inputs : {'sum', 'mean', 'max', 'min', 'diff'}, optional
        When assessing contractions, how to score the combination of the two
        input tensor sizes.
    coeff_subgraph_size : float, optional
        When assessing contractions, how to weight the total subgraph size
        corresponding to the inputs tensors.
    score_subgraph_size : {'sum', 'mean', 'max', 'min', 'diff'}, optional
        When assessing contractions, how to score the combination of the two
        input subgraph sizes.
    coeff_centrality : float, optional
        When assessing contractions, how to weight the combined centrality
        of the inputs tensors.
    centrality_combine : {'sum', 'mean', 'max', 'min'}, optional
        When performing the contraction, how to combine the two input tensor
        centralities to produce a new one.
    score_centrality : {'sum', 'mean', 'max', 'min', 'diff'}, optional
        When assessing contractions, how to score the combination of the two
        input tensor centralities.
    temperature : float, optional
        A noise level to apply to the scores when choosing nodes to expand to.
    """

    def __init__(
        self,
        chi,
        coeff_size_compressed=1.0,
        coeff_size=0.0,
        coeff_size_inputs=0.0,
        score_size_inputs='max',
        coeff_subgraph=0.0,
        score_subgraph='sum',
        coeff_centrality=0.0,
        centrality_combine='max',
        score_centrality='diff',
        temperature=0.0,
        score_perm='',
    ):
        self.chi = chi
        self.coeff_size_compressed = coeff_size_compressed
        self.coeff_size = coeff_size
        self.coeff_size_inputs = coeff_size_inputs
        self.score_size_inputs = score_size_inputs
        self.coeff_subgraph = coeff_subgraph
        self.score_subgraph = score_subgraph
        self.coeff_centrality = coeff_centrality
        self.centrality_combine = centrality_combine
        self.score_centrality = score_centrality
        self.temperature = temperature
        self.score_perm = score_perm

    def _score(self, i1, i2):
        # the two inputs tensors (with prior compressions)
        size1 = self.hg.node_size(i1)
        size2 = self.hg.node_size(i2)

        # the new tensor inds, plus indices that will be available to compress
        old_size = self.hg.candidate_contraction_size(i1, i2)
        new_size = self.hg.candidate_contraction_size(i1, i2, chi=self.chi)

        scores = {
            'R': self.coeff_size_compressed * math.log2(new_size),
            'O': self.coeff_size * math.log2(old_size),
            # weight some combination of the inputs sizes
            'I': self.coeff_size_inputs * _binary_combine(
                self.score_size_inputs, math.log2(size1), math.log2(size2)
            ),
            # weight some combination of the inputs subgraph sizes
            'S': self.coeff_subgraph * _binary_combine(
                self.score_subgraph,
                math.log(self.sgsizes[i1]),
                math.log(self.sgsizes[i2]),
            ),
            # weight some combination of the inputs centralities
            'L': self.coeff_centrality * _binary_combine(
                self.score_centrality, self.sgcents[i1], self.sgcents[i2]
            ),
            # randomize using boltzmann sampling trick
            'T': max(0.0, self.temperature) * gumbel(),
        }
        if self.score_perm == '':
            return sum(scores.values())
        return tuple(scores[p] for p in self.score_perm)

    def ssa_path(self, inputs, output, size_dict):
        self.candidates = []
        self.ssapath = []
        self.hg = get_hypergraph(inputs, output, size_dict, accel='auto')

        # compute hypergraph centralities to use heuristically
        self.sgcents = self.hg.simple_centrality()
        self.sgsizes = {i: 1 for i in range(len(inputs))}

        # populate initial scores with contractions among leaves
        for _, edge_nodes in self.hg.edges.items():
            for nodes in itertools.combinations(edge_nodes, 2):
                candidate = (self._score(*nodes), *nodes)
                heapq.heappush(self.candidates, candidate)

        while self.hg.get_num_nodes() > 2:

            if not self.candidates:
                # this occurs with disconneted sub-graphs -> pick any two
                i1, i2, *_ = self.hg.nodes
            else:
                # get the next best score contraction
                _, i1, i2 = heapq.heappop(self.candidates)

            if not (self.hg.has_node(i1) and self.hg.has_node(i2)):
                # invalid - either node already contracted
                continue

            # perform contraction
            i12 = self.hg.contract(i1, i2)
            self.hg.compress(chi=self.chi, edges=self.hg.get_node(i12))

            # build the path
            self.ssapath.append((i1, i2))

            # propagate some meta information up the contraction tree
            self.sgsizes[i12] = self.sgsizes.pop(i1) + self.sgsizes.pop(i2)
            self.sgcents[i12] = _binary_combine(self.centrality_combine,
                                                self.sgcents.pop(i1),
                                                self.sgcents.pop(i2))

            # assess / re-assess new and also neighboring contractions
            #     n.b. duplicate scores should be lower and heap-popped first
            for e in self.hg.neighbor_edges(i12):
                for nodes in itertools.combinations(self.hg.get_edge(e), 2):
                    candidate = (self._score(*nodes), *nodes)
                    heapq.heappush(self.candidates, candidate)

        self.ssapath.append(tuple(self.hg.nodes))

        return self.ssapath

    def __call__(self, inputs, output, size_dict, memory_limit=None):
        return ssa_to_linear(self.ssa_path(inputs, output, size_dict))


def greedy_compressed(inputs, output, size_dict, memory_limit=None, **kwargs):
    chi = max(size_dict.values())**2
    return GreedyCompressed(chi, **kwargs)(inputs, output, size_dict)


def trial_greedy_compressed(inputs, output, size_dict, **kwargs):
    opt = GreedyCompressed(**kwargs)
    ssa_path = opt.ssa_path(inputs, output, size_dict)
    tree = ContractionTree.from_path(
        inputs, output, size_dict, ssa_path=ssa_path)
    tree.set_surface_order_from_path(ssa_path)
    return tree


register_hyper_function(
    name='greedy-compressed',
    ssa_func=trial_greedy_compressed,
    space={
        'coeff_size_compressed': {'type': 'FLOAT', 'min': 0.5, 'max': 2.0},
        'coeff_size': {'type': 'FLOAT', 'min': 0.0, 'max': 1.0},
        'coeff_size_inputs': {'type': 'FLOAT', 'min': -1.0, 'max': 1.0},
        'score_size_inputs': {
            'type': 'STRING',
            'options': ['min', 'max', 'mean', 'sum', 'diff']},
        'coeff_subgraph': {'type': 'FLOAT', 'min': -1.0, 'max': 1.0},
        'score_subgraph': {
            'type': 'STRING',
            'options': ['min', 'max', 'mean', 'sum', 'diff']},
        'coeff_centrality': {'type': 'FLOAT', 'min': -10.0, 'max': 10.0},
        'centrality_combine': {
            'type': 'STRING',
            'options': ['min', 'max', 'mean']},
        'score_centrality': {
            'type': 'STRING',
            'options': ['min', 'max', 'mean', 'diff']},
        'temperature': {'type': 'FLOAT', 'min': -0.1, 'max': 1.0},
        'chi': {'type': 'INT', 'min': 2, 'max': 128},
    },
)


# --------------------------------------------------------------------------- #


class GreedySpan:
    """A contraction path optimizer that greedily generates spanning trees out
    of particular nodes, suitable for e.g. compressed contraction paths.

    Parameters
    ----------
    start : {'max', 'min'}, optional
        Whether to start the span from the maximum or minimum centrality point.
    coeff_connectivity : float, optional
        When considering adding nodes to the span, how to weight the nodes
        connectivity to the current span.
    coeff_ndim : float, optional
        When considering adding nodes to the span, how to weight the nodes
        total rank.
    coeff_distance : float, optional
        When considering adding nodes to the span, how to weight the nodes
        distance to the starting point.
    coeff_next_centrality : float, optional
        When considering adding nodes to the span, how to weight the nodes
        centrality.
    temperature : float, optional
        A noise level to apply to the scores when choosing nodes to expand to.
    """

    def __init__(
        self,
        start='max',
        coeff_connectivity=1.0,
        coeff_ndim=1.0,
        coeff_distance=-1.0,
        coeff_next_centrality=0.0,
        connectivity_weight_bonds=True,
        temperature=0.0,
        score_perm='CNDLTI',
        distance_p=1,
        distance_steal='abs',
    ):
        self.start = start
        self.coeff_connectivity = coeff_connectivity
        self.coeff_ndim = coeff_ndim
        self.coeff_distance = coeff_distance
        self.coeff_next_centrality = coeff_next_centrality
        self.connectivity_weight_bonds = connectivity_weight_bonds
        self.temperature = temperature
        self.score_perm = score_perm
        self.distance_p = distance_p
        self.distance_steal = distance_steal

    def ssa_path(self, inputs, output, size_dict):
        self.hg = get_hypergraph(inputs, output, size_dict, accel='auto')
        self.cents = self.hg.simple_centrality()

        def region_choose_sorter(node):
            return self.cents[node] + 1e-2 * random.random()

        if output:
            region = oset(self.hg.output_nodes())
        elif self.start == 'max':
            region = oset([max(self.cents.keys(), key=self.cents.__getitem__)])
        elif self.start == 'min':
            region = oset([min(self.cents.keys(), key=self.cents.__getitem__)])
        else:
            region = oset(self.start)

        candidates = []
        merges = {}
        distances = self.hg.simple_distance(list(region), p=self.distance_p)
        connectivity = collections.defaultdict(lambda: 0)

        if len(region) == 1:
            seq = []
        elif len(region) == 2:
            seq = [tuple(region)]
        else:
            # span will have multiple starting points, contract these
            o_nodes = list(region)
            o_inputs = [inputs[i] for i in o_nodes]
            o_ssa_path = ssa_greedy_optimize(o_inputs, output, size_dict)
            seq = []
            for pi, pj in o_ssa_path:
                merges[o_nodes[pi]] = o_nodes[pj]
                seq.append((o_nodes[pi], o_nodes[pj]))
                o_nodes.append(o_nodes[pj])
            seq.reverse()

        def _check_candidate(i_surface, i_neighbor):
            if (i_neighbor in region):
                return

            if i_neighbor in merges:
                i_current = merges[i_neighbor]

                if self.distance_steal == "abs":
                    if distances[i_surface] < distances[i_current]:
                        merges[i_neighbor] = i_surface

                elif self.distance_steal == 'rel':
                    old_diff = abs(distances[i_current] -
                                   distances[i_neighbor])
                    new_diff = abs(distances[i_surface] -
                                   distances[i_neighbor])
                    if new_diff > old_diff:
                        merges[i_neighbor] = i_surface
            else:
                merges[i_neighbor] = i_surface
                candidates.append(i_neighbor)

            if self.connectivity_weight_bonds:
                connectivity[i_neighbor] += math.log2(
                    self.hg.bond_size(i_surface, i_neighbor)
                )
            else:
                connectivity[i_neighbor] += 1

        def _sorter(i):
            scores = {
                'C': self.coeff_connectivity * connectivity[i],
                'N': self.coeff_ndim * len(inputs[i]),
                'D': self.coeff_distance * distances[i],
                'L': self.coeff_next_centrality * self.cents[i],
                'T': max(0.0, self.temperature) * gumbel(),
                'I': -i,
            }
            if self.score_perm == '':
                return sum(scores[o] for o in 'CNDLT')
            c = tuple(scores[o] for o in self.score_perm)
            return c

        for i in region:
            for j in self.hg.neighbors(i):
                _check_candidate(i, j)

        while candidates:
            candidates.sort(key=_sorter)
            i_surface = candidates.pop()
            region.add(i_surface)
            for i_next in self.hg.neighbors(i_surface):
                _check_candidate(i_surface, i_next)
            seq.append((i_surface, merges[i_surface]))
        seq.reverse()

        ssapath = []
        ssa = self.hg.get_num_nodes()
        node2ssa = {i: i for i in range(ssa)}
        for i, j in seq:
            ssapath.append((node2ssa[i], node2ssa[j]))
            node2ssa[j] = ssa
            ssa += 1

        return ssapath

    def __call__(self, inputs, output, size_dict, memory_limit=None):
        return ssa_to_linear(self.ssa_path(inputs, output, size_dict))


def greedy_span(inputs, output, size_dict, memory_limit=None, **kwargs):
    return GreedySpan(**kwargs)(inputs, output, size_dict)


def trial_greedy_span(inputs, output, size_dict, **kwargs):
    opt = GreedySpan(**kwargs)
    ssa_path = opt.ssa_path(inputs, output, size_dict)
    tree = ContractionTree.from_path(
        inputs, output, size_dict, ssa_path=ssa_path)
    tree.set_surface_order_from_path(ssa_path)
    return tree


_allowed_perms = tuple(
    "C" + "".join(p) + "T" for p in itertools.permutations('NDLI')
)


register_hyper_function(
    name='greedy-span',
    ssa_func=trial_greedy_span,
    space={
        'start': {'type': 'STRING', 'options': ['min', 'max']},
        'score_perm': {'type': 'STRING', 'options': _allowed_perms},
        'coeff_connectivity': {'type': 'INT', 'min': 0, 'max': 1},
        'coeff_ndim': {'type': 'INT', 'min': -1, 'max': 1},
        'coeff_distance': {'type': 'INT', 'min': -1, 'max': 1},
        'coeff_next_centrality': {'type': 'FLOAT', 'min': -1, 'max': 1},
        'connectivity_weight_bonds': {'type': 'BOOL'},
        'temperature': {'type': 'FLOAT', 'min': -1.0, 'max': 1.0},
        'distance_p': {'type': 'FLOAT', 'min': -5.0, 'max': 5.0},
        'distance_steal': {'type': 'STRING', 'options': ['', 'abs', 'rel']},
    },
)
