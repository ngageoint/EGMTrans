"""Tile adjacency, cross-tile water-body merging, and the boundary report.

Within one tile a flat patch is identified by its label.  Two patches in
neighboring tiles are the same water body when a labeled post of one is the
same post as (overlapping tiles such as DTED), or 4-adjacent across the seam to
(abutting tiles such as Copernicus GeoTIFF), a labeled post of the other with
the same input height.  Only the posts along the four edges of each tile are
recorded, so the state kept for a run of thousands of tiles is small.

Nothing here reads a raster: the functions work on label arrays, geotransforms
and the small records built from them, which keeps them testable without grids.
"""

from __future__ import annotations

import math
import os
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from egmtrans.io import round_half_away

SIDES = ('N', 'S', 'W', 'E')
OPPOSITE = {'N': 'S', 'S': 'N', 'W': 'E', 'E': 'W'}

# Post positions are compared as fractions of a post spacing: 1e-3 is far above
# floating-point noise in a coordinate and far below any real offset.
EDGE_TOLERANCE_FRACTION = 1e-3
# Edge lines are keyed for table lookups at this resolution (0.1 m in degrees).
LINE_QUANTUM = 1e-6


def post_centers(
    geotransform: tuple[float, float, float, float, float, float], rows: int, cols: int
) -> tuple[np.ndarray, np.ndarray]:
    """The x of every column center and the y of every row center.

    This is the formula ``create_datum_array`` uses for its query points, so
    edge coordinates match the posts the transform actually evaluated.
    """
    x0, dx, _, y0, _, dy = geotransform
    x = x0 + (np.arange(cols) + 0.5) * dx
    y = y0 + (np.arange(rows) + 0.5) * dy
    return x, y


@dataclass(eq=False)
class EdgeRecord:
    """The labeled posts along one side of a tile.

    Attributes:
        side: ``'N'``, ``'S'``, ``'W'`` or ``'E'``.
        across: The coordinate of the edge line: y of the edge row, x of the edge column.
        along0: The along-edge coordinate of index 0 (x of column 0, y of row 0).
        step: The signed spacing along the edge (dx for N/S, dy for W/E, so negative).
        length: Posts along the edge.
        idx: Positions along the edge whose label is above 1 (ocean is level 0
            everywhere and never needs merging), sorted.
        labels: The label at each position of *idx*.
    """

    side: str
    across: float
    along0: float
    step: float
    length: int
    idx: np.ndarray
    labels: np.ndarray

    def coordinate(self, index: float) -> float:
        return self.along0 + index * self.step

    def coordinates(self) -> np.ndarray:
        return self.along0 + self.idx.astype(np.float64) * self.step

    def positions_of(self, label: int) -> np.ndarray:
        return self.idx[self.labels == label]


@dataclass
class PatchStats:
    """What pass 1 keeps about one edge-touching patch of one tile.

    *above* and *below* count the patch's boundary neighbors in the input
    that lie above and below it (see
    :func:`~egmtrans.flattening.patch_boundary_stats`); summed over a water
    body they decide whether it is water at all.
    """

    height_cm: int
    min_value: float
    count: int
    above: int = 0
    below: int = 0


@dataclass(eq=False)
class TileAnalysis:
    """The per-tile record that survives pass 1."""

    tile_id: int
    input_file: str
    output_file: str | None  # None for a context tile: analyzed, never written
    crs_key: str
    geotransform: tuple[float, float, float, float, float, float]
    rows: int
    cols: int
    is_dted: bool
    edges: dict[str, EdgeRecord] = field(default_factory=dict)
    patches: dict[int, PatchStats] = field(default_factory=dict)
    error: str | None = None

    @property
    def name(self) -> str:
        return os.path.basename(self.input_file)

    @property
    def is_context(self) -> bool:
        return self.output_file is None

    @property
    def dx(self) -> float:
        return self.geotransform[1]

    @property
    def dy(self) -> float:
        return abs(self.geotransform[5])

    @property
    def x_west(self) -> float:
        return float(post_centers(self.geotransform, self.rows, self.cols)[0][0])

    @property
    def x_east(self) -> float:
        return float(post_centers(self.geotransform, self.rows, self.cols)[0][-1])

    @property
    def y_north(self) -> float:
        return float(post_centers(self.geotransform, self.rows, self.cols)[1][0])

    @property
    def y_south(self) -> float:
        return float(post_centers(self.geotransform, self.rows, self.cols)[1][-1])


@dataclass(eq=False)
class Seam:
    """One shared or abutting edge between two tiles.

    ``a_idx[k]`` and ``b_idx[k]`` are the positions of the k-th pair of
    labeled posts that are the same post or 4-adjacent across the seam.
    ``a_cover`` is the index interval of A's edge that B's edge spans (and vice
    versa); a labeled post outside every cover of its edge faces no tile.
    """

    a: int
    a_side: str
    b: int
    b_side: str
    a_idx: np.ndarray
    b_idx: np.ndarray
    a_cover: tuple[int, int] | None
    b_cover: tuple[int, int] | None


@dataclass
class Crossing:
    """A run of labeled posts of one water body along one tile edge."""

    tile_id: int
    side: str
    line: float
    start: float
    end: float

    def overlaps(self, start: float, end: float, tolerance: float) -> bool:
        return self.end >= start - tolerance and self.start <= end + tolerance


@dataclass
class WaterBody:
    """A merged flat area: every edge-touching patch it consists of, and its level.

    *water* is False when too little of its boundary lies above it (a contour
    band on a slope, a flat hilltop); such a body is not flattened, reported
    or exported.
    """

    height_cm: int
    level: float
    posts: int
    members: list[tuple[int, int]]  # (tile_id, label)
    open_edges: list[tuple[int, str]] = field(default_factory=list)  # (tile_id, side)
    crossings: list[Crossing] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    table_level: float | None = None
    above: int = 0
    below: int = 0
    water: bool = True

    @property
    def tile_ids(self) -> list[int]:
        return sorted({tile_id for tile_id, _ in self.members})

    @property
    def containment(self) -> float:
        """Share of the boundary above the body, NaN when there is none to compare."""
        total = self.above + self.below
        return self.above / total if total else float('nan')


@dataclass
class TileLevels:
    """What pass 2 receives for one tile.

    *levels* holds the merged level of every edge-touching label that is
    water; *not_water* the edge-touching labels the merge decided are not
    (their patches are left as terrain); *expected_counts* the post count of
    every edge-touching label when the tile was analyzed, so pass 2 can check
    it saw the same input.
    """

    levels: dict[int, float] = field(default_factory=dict)
    expected_counts: dict[int, int] = field(default_factory=dict)
    not_water: set[int] = field(default_factory=set)

    def override_array(self, max_label: int) -> np.ndarray:
        """A dense float array indexed by label, NaN where there is no override."""
        override = np.full(max_label + 1, np.nan)
        for label, level in self.levels.items():
            if label <= max_label:
                override[label] = level
        return override


@dataclass
class TableRow:
    height_cm: int
    level: float
    posts: int
    tiles: int
    side: str
    line: float
    start: float
    end: float


class WaterLevelTable:
    """Levels exported by an earlier run, looked up by edge crossing.

    A partial run matches its own crossings against the table by the side and
    coordinate of the edge line, the input height (to 1 cm either way), and an
    overlap of the along-edge intervals.
    """

    def __init__(self, rows: list[TableRow], source_datum: str, target_datum: str, version: str = ''):
        self.rows = rows
        self.source_datum = source_datum
        self.target_datum = target_datum
        self.version = version
        self._index: dict[tuple[str, int, int], list[TableRow]] = defaultdict(list)
        for row in rows:
            self._index[(row.side, _line_key(row.line), row.height_cm)].append(row)

    def lookup(self, side: str, line: float, height_cm: int) -> list[TableRow]:
        key = _line_key(line)
        found = []
        for k in (key - 1, key, key + 1):
            for h in (height_cm - 1, height_cm, height_cm + 1):
                found.extend(self._index.get((side, k, h), []))
        return found


def _line_key(line: float) -> int:
    return int(round(line / LINE_QUANTUM))


class DisjointSet:
    """Union-find with path halving; unions attach the larger key under the smaller."""

    def __init__(self):
        self._parent: dict = {}

    def add(self, item) -> None:
        self._parent.setdefault(item, item)

    def find(self, item):
        parent = self._parent
        root = item
        while parent[root] != root:
            root = parent[root]
        while parent[item] != root:
            parent[item], item = root, parent[item]
        return root

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if rb < ra:
            ra, rb = rb, ra
        self._parent[rb] = ra

    def groups(self) -> dict:
        members: dict = defaultdict(list)
        for item in self._parent:
            members[self.find(item)].append(item)
        return {root: sorted(items) for root, items in sorted(members.items())}


# ---------------------------------------------------------------------------
# Per-tile records
# ---------------------------------------------------------------------------


def _edge_frames(geotransform: tuple, rows: int, cols: int) -> dict[str, tuple[float, float, float, int]]:
    """``(across, along0, step, length)`` of each side of a tile."""
    x, y = post_centers(geotransform, rows, cols)
    dx, dy = geotransform[1], geotransform[5]
    return {
        'N': (float(y[0]), float(x[0]), float(dx), cols),
        'S': (float(y[-1]), float(x[0]), float(dx), cols),
        'W': (float(x[0]), float(y[0]), float(dy), rows),
        'E': (float(x[-1]), float(y[0]), float(dy), rows),
    }


def geometry_edges(geotransform: tuple, rows: int, cols: int) -> dict[str, EdgeRecord]:
    """The four edges of a tile with no labeled posts: enough to find its seams."""
    empty = np.empty(0, dtype=np.int32)
    return {
        side: EdgeRecord(side, across, along0, step, length, empty, empty)
        for side, (across, along0, step, length) in _edge_frames(geotransform, rows, cols).items()
    }


def extract_edges(labeled: np.ndarray, geotransform: tuple) -> dict[str, EdgeRecord]:
    """The labeled posts (label > 1) along the four edges of a label array."""
    rows, cols = labeled.shape
    lines = {'N': labeled[0, :], 'S': labeled[-1, :], 'W': labeled[:, 0], 'E': labeled[:, -1]}
    edges = {}
    for side, (across, along0, step, length) in _edge_frames(geotransform, rows, cols).items():
        line = lines[side]
        idx = np.flatnonzero(line > 1).astype(np.int32)
        edges[side] = EdgeRecord(side, across, along0, step, length, idx, line[idx].astype(np.int32))
    return edges


def edge_touching_labels(edges: dict[str, EdgeRecord]) -> np.ndarray:
    """Every label above 1 that appears on any edge, sorted."""
    labels = [edge.labels for edge in edges.values() if edge.labels.size]
    if not labels:
        return np.empty(0, dtype=np.int32)
    return np.unique(np.concatenate(labels)).astype(np.int32)


def edge_post(side: str, position: int, rows: int, cols: int) -> tuple[int, int]:
    """Row and column of a position along an edge."""
    if side == 'N':
        return 0, position
    if side == 'S':
        return rows - 1, position
    if side == 'W':
        return position, 0
    return position, cols - 1


def edge_patch_stats(
    input_array: np.ndarray,
    edges: dict[str, EdgeRecord],
    levels: np.ndarray,
    counts: np.ndarray,
    above: np.ndarray | None = None,
    below: np.ndarray | None = None,
) -> dict[int, PatchStats]:
    """Height, minimum transformed value, post count and boundary counts of every edge-touching patch."""
    rows, cols = input_array.shape
    stats: dict[int, PatchStats] = {}
    for side, edge in edges.items():
        for position, label in zip(edge.idx.tolist(), edge.labels.tolist()):
            if label in stats:
                continue
            i, j = edge_post(side, position, rows, cols)
            stats[label] = PatchStats(
                height_cm=int(round(float(input_array[i, j]) * 100)),
                min_value=float(levels[label]),
                count=int(counts[label]),
                above=int(above[label]) if above is not None else 0,
                below=int(below[label]) if below is not None else 0,
            )
    return stats


def connected_tiles(seams: list[Seam], roots: set[int]) -> set[int]:
    """The tile ids reachable from *roots* over the seams, *roots* included.

    A water body can only reach a tile through a chain of seams, so context
    tiles outside this set cannot change a level of the run.
    """
    neighbors: dict[int, set[int]] = defaultdict(set)
    for seam in seams:
        neighbors[seam.a].add(seam.b)
        neighbors[seam.b].add(seam.a)
    reached = set(roots)
    frontier = list(roots)
    while frontier:
        tile_id = frontier.pop()
        for other in neighbors.get(tile_id, ()):
            if other not in reached:
                reached.add(other)
                frontier.append(other)
    return reached


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------


def _cover(ea: EdgeRecord, eb: EdgeRecord) -> tuple[int, int] | None:
    """The index interval of *ea* spanned by the whole of *eb*, or None if disjoint."""
    k0 = (eb.along0 - ea.along0) / ea.step
    k1 = (eb.coordinate(eb.length - 1) - ea.along0) / ea.step
    lo, hi = min(k0, k1), max(k0, k1)
    start = max(0, math.ceil(lo - EDGE_TOLERANCE_FRACTION))
    end = min(ea.length - 1, math.floor(hi + EDGE_TOLERANCE_FRACTION))
    return (start, end) if start <= end else None


def _match_edges(ea: EdgeRecord, eb: EdgeRecord) -> tuple[np.ndarray, np.ndarray]:
    """Pairs of labeled positions on the two edges that share an along-edge coordinate."""
    empty = np.empty(0, dtype=np.int32)
    if ea.idx.size == 0 or eb.idx.size == 0:
        return empty, empty
    k = (eb.coordinates() - ea.along0) / ea.step
    k_round = np.rint(k)
    near = (np.abs(k - k_round) <= EDGE_TOLERANCE_FRACTION) & (k_round >= 0) & (k_round < ea.length)
    k_round = k_round[near].astype(np.int64)
    b_positions = eb.idx[near]
    pos = np.searchsorted(ea.idx, k_round)
    inside = pos < ea.idx.size
    pos = np.minimum(pos, ea.idx.size - 1)
    hit = inside & (ea.idx[pos] == k_round)
    return k_round[hit].astype(np.int32), b_positions[hit].astype(np.int32)


def find_seams(tiles: list[TileAnalysis]) -> list[Seam]:
    """Every shared or abutting edge between the tiles, each found once.

    Tiles must share a CRS.  Two tiles are east-west neighbors when the west
    column of one is the east column of the other (overlapping products) or
    one post spacing east of it (abutting products), and their rows overlap;
    north-south neighbors likewise.  Posts along the seam are matched by
    coordinate, so a coarser neighbor (a DTED2 tile above 50 degrees has half
    the columns) matches every second post of the finer edge.
    """
    usable = [t for t in tiles if t.error is None]
    seams: list[Seam] = []

    def add(a: TileAnalysis, a_side: str, b: TileAnalysis, b_side: str) -> None:
        ea, eb = a.edges[a_side], b.edges[b_side]
        a_idx, b_idx = _match_edges(ea, eb)
        seams.append(Seam(a.tile_id, a_side, b.tile_id, b_side, a_idx, b_idx, _cover(ea, eb), _cover(eb, ea)))

    by_west = sorted(usable, key=lambda t: t.x_west)
    x_west = np.array([t.x_west for t in by_west])
    for a in usable:
        tol = EDGE_TOLERANCE_FRACTION * a.dx
        lo = int(np.searchsorted(x_west, a.x_east - tol))
        hi = int(np.searchsorted(x_west, a.x_east + a.dx + tol, side='right'))
        for b in by_west[lo:hi]:
            if b is a or b.crs_key != a.crs_key:
                continue
            gap = b.x_west - a.x_east
            tol = EDGE_TOLERANCE_FRACTION * min(a.dx, b.dx)
            if not (abs(gap) <= tol or abs(gap - a.dx) <= tol):
                continue
            if a.y_south > b.y_north + tol or b.y_south > a.y_north + tol:
                continue
            add(a, 'E', b, 'W')

    by_north = sorted(usable, key=lambda t: -t.y_north)
    y_north = np.array([-t.y_north for t in by_north])
    for a in usable:
        tol = EDGE_TOLERANCE_FRACTION * a.dy
        lo = int(np.searchsorted(y_north, -(a.y_south + tol)))
        hi = int(np.searchsorted(y_north, -(a.y_south - a.dy - tol), side='right'))
        for b in by_north[lo:hi]:
            if b is a or b.crs_key != a.crs_key:
                continue
            gap = a.y_south - b.y_north
            tol = EDGE_TOLERANCE_FRACTION * min(a.dy, b.dy)
            if not (abs(gap) <= tol or abs(gap - a.dy) <= tol):
                continue
            if a.x_east < b.x_west - tol or b.x_east < a.x_west - tol:
                continue
            add(a, 'S', b, 'N')

    return seams


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def _runs(positions: np.ndarray) -> list[tuple[int, int]]:
    """Maximal runs of consecutive positions as (first, last) pairs."""
    if positions.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(positions) != 1)
    starts = positions[np.concatenate(([0], breaks + 1))]
    ends = positions[np.concatenate((breaks, [positions.size - 1]))]
    return list(zip(starts.tolist(), ends.tolist()))


def _all_covered(positions: np.ndarray, covers: list[tuple[int, int]]) -> bool:
    covered = np.zeros(positions.size, dtype=bool)
    for start, end in covers:
        covered |= (positions >= start) & (positions <= end)
    return bool(covered.all())


def _apply_table(body: WaterBody, table: WaterLevelTable, spacing: float) -> None:
    matches = []
    for crossing in body.crossings:
        for row in table.lookup(crossing.side, crossing.line, body.height_cm):
            if crossing.overlaps(row.start, row.end, EDGE_TOLERANCE_FRACTION * spacing):
                matches.append(row.level)
    if not matches:
        return
    table_level = min(matches)
    if max(matches) - table_level > 1e-9:
        body.notes.append(
            f"water-level table rows for this body disagree ({table_level:.3f} to {max(matches):.3f} m); "
            f"the lowest was used"
        )
    if table_level < body.level - 1e-9:
        body.table_level = table_level
        body.level = table_level
    elif table_level > body.level + 1e-9:
        body.notes.append(
            f"the water-level table gives {table_level:.3f} m but this run's own minimum is {body.level:.3f} m; "
            f"the table may be stale or from a partial run, the lower value was kept"
        )
    else:
        body.table_level = table_level


def merge_patches(
    tiles: list[TileAnalysis],
    seams: list[Seam],
    table: WaterLevelTable | None = None,
    min_containment: float = 0.8,
) -> tuple[dict[int, TileLevels], list[WaterBody]]:
    """Join edge-touching patches across seams and give each water body one level.

    Two patches are joined when a matched pair of posts on a seam carries their
    labels and their input heights agree within 1 cm.  A body is water when at
    least *min_containment* of its boundary, summed over all its parts, lies
    above it; the decision is made for the whole body so that the two sides of
    a seam agree.  The level of a water body is the minimum over its members
    (and over a matching table row, which can lower it but never raise it).
    Returns the levels every output tile needs in pass 2, and the bodies sorted
    largest first.
    """
    by_id = {t.tile_id: t for t in tiles}
    sets = DisjointSet()
    for tile in tiles:
        if tile.error is None:
            for label in tile.patches:
                sets.add((tile.tile_id, label))

    covers: dict[tuple[int, str], list[tuple[int, int]]] = defaultdict(list)
    height_conflicts: list[str] = []
    for seam in seams:
        if seam.a_cover:
            covers[(seam.a, seam.a_side)].append(seam.a_cover)
        if seam.b_cover:
            covers[(seam.b, seam.b_side)].append(seam.b_cover)
        a, b = by_id[seam.a], by_id[seam.b]
        ea, eb = a.edges[seam.a_side], b.edges[seam.b_side]
        labels_a = ea.labels[np.searchsorted(ea.idx, seam.a_idx)]
        labels_b = eb.labels[np.searchsorted(eb.idx, seam.b_idx)]
        for la, lb in set(zip(labels_a.tolist(), labels_b.tolist())):
            pa, pb = a.patches.get(la), b.patches.get(lb)
            if pa is None or pb is None:
                continue
            if abs(pa.height_cm - pb.height_cm) <= 1:
                sets.union((seam.a, la), (seam.b, lb))
            else:
                height_conflicts.append(
                    f"{a.name}:{seam.a_side} is {pa.height_cm / 100:.2f} m where {b.name}:{seam.b_side} is "
                    f"{pb.height_cm / 100:.2f} m; the two were kept separate"
                )

    bodies: list[WaterBody] = []
    for members in sets.groups().values():
        stats = [by_id[tile_id].patches[label] for tile_id, label in members]
        body = WaterBody(
            height_cm=stats[0].height_cm,
            level=min(s.min_value for s in stats),
            posts=sum(s.count for s in stats),
            members=members,
            above=sum(s.above for s in stats),
            below=sum(s.below for s in stats),
        )
        containment = body.containment
        body.water = bool(math.isnan(containment) or containment >= min_containment)
        for tile_id, label in members:
            tile = by_id[tile_id]
            for side, edge in tile.edges.items():
                positions = edge.positions_of(label)
                if positions.size == 0:
                    continue
                for first, last in _runs(positions):
                    start, end = sorted((edge.coordinate(first), edge.coordinate(last)))
                    body.crossings.append(Crossing(tile_id, side, edge.across, start, end))
                if not _all_covered(positions, covers.get((tile_id, side), [])):
                    body.open_edges.append((tile_id, side))
        if table is not None and body.water:
            spacing = min(by_id[tile_id].dx for tile_id, _ in members)
            _apply_table(body, table, spacing)
        bodies.append(body)
    bodies.sort(key=lambda b: (not b.water, -b.posts, b.height_cm, b.members[0]))

    levels: dict[int, TileLevels] = {
        t.tile_id: TileLevels() for t in tiles if t.error is None and not t.is_context
    }
    for body in bodies:
        for tile_id, label in body.members:
            if tile_id in levels:
                if body.water:
                    levels[tile_id].levels[label] = body.level
                else:
                    levels[tile_id].not_water.add(label)
                levels[tile_id].expected_counts[label] = by_id[tile_id].patches[label].count
    if height_conflicts and bodies:
        bodies[0].notes.extend(height_conflicts)
    return levels, bodies


def single_tile_water_bodies(tile: TileAnalysis, min_containment: float = 0.8) -> list[WaterBody]:
    """The edge-touching water bodies of a tile processed on its own: every touched side is open."""
    return [body for body in merge_patches([tile], [], None, min_containment)[1] if body.water]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def format_boundary_report(bodies: list[WaterBody], tiles: list[TileAnalysis], limit: int = 100) -> list[str]:
    """Lines describing the water bodies that touch an edge with no neighbor in the run."""
    by_id = {t.tile_id: t for t in tiles}
    bodies = [b for b in bodies if b.water]
    open_bodies = [b for b in bodies if b.open_edges]
    spanning = sum(1 for b in bodies if len(b.tile_ids) > 1)
    lines: list[str] = []
    if open_bodies:
        lines.append(
            "Water bodies touching an edge of this run. A neighboring tile transformed separately "
            "may get a different level:"
        )
        lines.append(f"  {'height (in)':>12}   {'level (out)':<18} {'tiles':>5} {'posts':>10}   open edges")
        for body in open_bodies[:limit]:
            tile = by_id[body.members[0][0]]
            level = f"{body.level:.2f} m"
            if tile.is_dted:
                level += f" ({int(round_half_away(np.array(body.level)))} m)"
            if body.table_level is not None:
                level += " [table]"
            edges: dict[int, list[str]] = defaultdict(list)
            for tile_id, side in body.open_edges:
                edges[tile_id].append(side)
            named = []
            for tile_id in sorted(edges):
                t = by_id[tile_id]
                named.append(f"{t.name}:{','.join(edges[tile_id])}" + (" (context)" if t.is_context else ""))
            lines.append(
                f"  {body.height_cm / 100:>10.2f} m   {level:<18} {len(body.tile_ids):>5} {body.posts:>10,}   "
                + "  ".join(named)
            )
        if len(open_bodies) > limit:
            lines.append(f"  ... and {len(open_bodies) - limit} more")
    lines.append(
        f"{spanning} water bod{'y spans' if spanning == 1 else 'ies span'} more than one tile and "
        f"{'was' if spanning == 1 else 'were'} set to one level each; {len(open_bodies)} touch the run boundary."
    )
    for body in bodies:
        for note in body.notes:
            lines.append(f"  note: water body at {body.height_cm / 100:.2f} m: {note}")
    return lines
