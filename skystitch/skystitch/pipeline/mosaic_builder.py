"""
mosaic_builder.py
==================
Pipeline stages 3-5:
  - chain_transforms      : chains homographies between photo pairs (from
                             feature matching) into one shared "pixel
                             space" (relative to one reference photo)
  - fit_world_similarity  : finds the transform (scale + rotation +
                             translation) that turns that shared pixel
                             space into real-world coordinates (meters),
                             based on each photo's actual GPS position
  - render_mosaic         : warps all photos onto one canvas & blends the
                             overlapping areas
"""

from collections import deque

import cv2
import numpy as np


class UnreliableGeoreferenceError(Exception):
    """Raised when there isn't enough information (GPS baseline, analytic
    GSD from camera EXIF, etc.) to fit a trustworthy pixel->world
    transform. Callers should surface this as a clear failure instead of
    letting a guessed/arbitrary transform silently produce a GeoTIFF that
    looks georeferenced but doesn't actually match reality."""


def chain_transforms(n, edges):
    """Chain homographies between photo pairs (from feature matching) into
    a transform for each photo -> a shared 'reference pixel space' (the
    pixel space of the root photo, before GPS correction). The root is
    chosen as the photo with the most matching connections (most stable
    to use as a reference).

    edges: {(i,j): (H_ij, n_inliers)} ; H_ij maps pixels of photo j -> pixels of photo i
    Returns: root_index, transforms (list of 3x3 matrices or None), connected (list of bool)
    """
    adjacency = {i: [] for i in range(n)}
    for (i, j), (H, n_inl) in edges.items():
        adjacency[i].append((j, np.linalg.inv(H), n_inl))  # i -> j using inverse of H_ij
        adjacency[j].append((i, H, n_inl))  # j -> i using H_ij directly

    # choose root = the node with the highest total inlier count (strongest/most connections)
    strength = {i: sum(w for _, _, w in adjacency[i]) for i in range(n)}
    root = max(strength, key=strength.get) if any(strength.values()) else 0

    transforms = [None] * n
    connected = [False] * n
    transforms[root] = np.eye(3)
    connected[root] = True

    queue = deque([root])
    while queue:
        cur = queue.popleft()
        for neighbor, H_cur_to_neighbor, _ in adjacency[cur]:
            if connected[neighbor]:
                continue
            # transforms[cur] maps pixel(cur) -> reference space
            # H_cur_to_neighbor maps pixel(cur) -> pixel(neighbor) -- needs to be inverted
            H_neighbor_to_cur = np.linalg.inv(H_cur_to_neighbor)
            transforms[neighbor] = transforms[cur] @ H_neighbor_to_cur
            connected[neighbor] = True
            queue.append(neighbor)

    return root, transforms, connected


def _umeyama_2d_weighted(src_pts, dst_pts, weights):
    """Weighted version of the similarity fit below: same transform
    (uniform scale + rotation/reflection + translation), but each
    correspondence contributes to the fit according to `weights` (all
    equal weights reproduce the plain unweighted fit exactly). This is
    what lets surveyed Ground Control Points (see pipeline/gcp_icp.py) be
    trusted more than ordinary consumer-GPS photo positions when both are
    mixed into the same fit -- see fit_world_similarity's GCP/ICP
    refinement below.

    ABOUT THE REFLECTION: `src_pts` live in "reference pixel space" (row
    increases DOWNWARDS -- normal image/screen convention), while
    `dst_pts` are real-world coordinates from pyproj/UTM (Y increases
    NORTHWARDS -- normal map convention). Those two conventions have
    opposite handedness, so the correct transform between them always
    includes exactly one reflection (a mirrored axis) in addition to any
    rotation -- it's a structural fact about the two coordinate systems,
    not something that depends on the particular GPS points. The fit
    below is left unconstrained (allowed to be a reflection) for exactly
    this reason, matching the two GPS-fallback branches elsewhere in this
    function, which already deliberately negate the Y scale (`M[1, 1] =
    -s`) for the same reason.
    """
    src = np.asarray(src_pts, dtype=np.float64)
    dst = np.asarray(dst_pts, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)
    w_sum = w.sum()
    if w_sum <= 1e-12:
        w = np.ones_like(w)
        w_sum = w.sum()

    mu_src = (w[:, None] * src).sum(axis=0) / w_sum
    mu_dst = (w[:, None] * dst).sum(axis=0) / w_sum
    src_c = src - mu_src
    dst_c = dst - mu_dst

    var_src = (w * (src_c ** 2).sum(axis=1)).sum() / w_sum
    cov = ((dst_c * w[:, None]).T @ src_c) / w_sum

    U, D, Vt = np.linalg.svd(cov)
    R = U @ Vt  # best-fit orthogonal matrix -- rotation OR reflection, whichever the data needs
    scale = D.sum() / var_src if var_src > 1e-9 else 1.0
    t = mu_dst - scale * (R @ mu_src)

    M = np.eye(3)
    M[:2, :2] = scale * R
    M[:2, 2] = t
    return M


def _umeyama_2d(src_pts, dst_pts):
    """Find the best 2D similarity transform (uniform scale + rotation, and
    a reflection if the data calls for it, + translation) mapping
    src_pts -> dst_pts (least squares, Umeyama/Procrustes). Equivalent to
    `_umeyama_2d_weighted` with all weights equal to 1."""
    n = len(src_pts)
    return _umeyama_2d_weighted(src_pts, dst_pts, np.ones(n, dtype=np.float64))


# --- GCP/ICP refinement -----------------------------------------------
# How many reweighting iterations to run when GCPs are present, and the
# convergence tolerance (meters of change in the GCPs' own RMS residual
# between iterations) at which to stop early.
GCP_ICP_MAX_ITERATIONS = 6
GCP_ICP_CONVERGENCE_M = 0.01


def _fit_similarity_gcp_icp(ref_pts, world_pts, weights, is_gcp, feedback=print):
    """Refine the similarity transform using surveyed Ground Control
    Points (GCPs) as trusted anchors, with an iterative reweighting pass
    in the spirit of Iterative Closest Point: each iteration re-fits the
    transform, then down-weights ordinary GPS-derived photo positions
    whose residual disagrees strongly with the GCP-anchored fit (likely
    plain consumer-GPS noise), and repeats until the GCPs' own residual
    stabilizes. Note this differs from textbook point-cloud ICP in that
    correspondences are already known (GCPs are matched to specific
    photos by filename beforehand, see pipeline/gcp_icp.py) -- only the
    transform and the per-point trust weights are iterated here, not the
    correspondences themselves.
    """
    cur_weights = weights.copy()
    prev_gcp_rms = None
    M = _umeyama_2d_weighted(ref_pts, world_pts, cur_weights)

    for iteration in range(GCP_ICP_MAX_ITERATIONS):
        M = _umeyama_2d_weighted(ref_pts, world_pts, cur_weights)

        fitted = (M[:2, :2] @ ref_pts.T).T + M[:2, 2]
        residuals = np.linalg.norm(fitted - world_pts, axis=1)
        gcp_rms = float(np.sqrt(np.mean(residuals[is_gcp] ** 2))) if is_gcp.any() else 0.0

        feedback(
            f"        GCP/ICP refinement iteration {iteration + 1}/{GCP_ICP_MAX_ITERATIONS}: "
            f"GCP RMS residual = {gcp_rms:.3f} m"
        )

        if prev_gcp_rms is not None and abs(prev_gcp_rms - gcp_rms) < GCP_ICP_CONVERGENCE_M:
            break
        prev_gcp_rms = gcp_rms

        non_gcp = ~is_gcp
        if is_gcp.any() and non_gcp.any():
            gps_residuals = residuals[non_gcp]
            threshold = max(gcp_rms * 3.0, 1.0)
            cur_weights[non_gcp] = np.where(
                gps_residuals > threshold,
                weights[non_gcp] * (threshold / gps_residuals),
                weights[non_gcp],
            )

    feedback(f"        Final GCP RMS residual after GCP/ICP refinement: {prev_gcp_rms:.3f} m")
    return M


def fit_world_similarity(images, rel_transforms, connected, world_xy, analytic_gsd=None,
                          root_yaw_deg=None, point_weights=None, gcp_mask=None, feedback=print):
    """Compute the transform from 'reference pixel space' -> real-world
    coordinates (meters). If the GPS baseline between photos is too tight
    (< 8m, below consumer GPS accuracy), the scale is forced to use the
    analytic estimate from camera parameters, and GPS is only used to
    determine position (translation).

    root_yaw_deg: compass heading (degrees, 0 = north, clockwise-positive)
    that the TOP edge of the root photo (the photo at the origin of
    'reference pixel space', i.e. rel_transforms[root] == identity) points
    towards -- typically read from the drone's gimbal/flight yaw in the
    photo's XMP metadata. Only used in the tight-GPS-baseline fallback
    below: with too little GPS spread to fit rotation from the data,
    orientation would otherwise default to zero (photo 'up' = world
    north), which is only right by coincidence. A known gimbal yaw gives a
    real orientation estimate instead of that default guess. Pass None to
    keep the previous zero-rotation behavior (e.g. camera/EXIF doesn't
    expose gimbal yaw).

    point_weights / gcp_mask: optional, both aligned with `images`/
    `connected` (i.e. indexed by photo, not pre-filtered). `gcp_mask[i]`
    True marks photo i's position in `world_xy` as a surveyed Ground
    Control Point rather than an ordinary GPS reading -- see
    pipeline/gcp_icp.py. When any GCPs are present, an iterative
    (ICP-style) reweighting refinement is used instead of the plain
    one-shot similarity fit. Pass None for GPS-only behavior identical to
    before."""
    ref_pts = []
    world_pts = []
    weights = []
    is_gcp = []
    for i, img in enumerate(images):
        if not connected[i]:
            continue
        h, w = img.shape[:2]
        center = np.array([w / 2.0, h / 2.0, 1.0])
        ref_center = rel_transforms[i] @ center
        ref_center = ref_center[:2] / ref_center[2]
        ref_pts.append(ref_center)
        world_pts.append(world_xy[i])
        weights.append(point_weights[i] if point_weights is not None else 1.0)
        is_gcp.append(bool(gcp_mask[i]) if gcp_mask is not None else False)

    ref_pts = np.array(ref_pts)
    world_pts = np.array(world_pts)
    weights = np.array(weights, dtype=np.float64)
    is_gcp = np.array(is_gcp, dtype=bool)

    MIN_RELIABLE_BASELINE_M = 8.0

    baseline = 0.0
    if len(world_pts) >= 2:
        d = world_pts[:, None, :] - world_pts[None, :, :]
        baseline = float(np.sqrt((d ** 2).sum(axis=-1)).max())

    use_analytic = analytic_gsd is not None and (len(ref_pts) < 2 or baseline < MIN_RELIABLE_BASELINE_M)

    if use_analytic:
        feedback(
            f"        [WARNING] GPS distance between photos is only ~{baseline:.1f} m (too tight "
            f"for accurate scale calibration). Using GSD estimate from camera parameters instead: "
            f"{analytic_gsd:.4f} m/px."
        )
        mu_ref = ref_pts.mean(axis=0)
        mu_world = world_pts.mean(axis=0)
        s = analytic_gsd

        if root_yaw_deg is not None:
            # Rotate by the root photo's known heading instead of assuming
            # 'photo up = north'. Image coords have x=right, y=down, while
            # world coords have x=east, y=north, so on top of the rotation
            # there's also the same axis reflection as the zero-rotation
            # case below (yaw=0 reduces exactly to diag(s, -s), i.e. the
            # same matrix the old code always used).
            feedback(f"        Using root photo's gimbal yaw ({root_yaw_deg:.1f} deg) for orientation.")
            yaw = np.radians(root_yaw_deg)
            cos_y, sin_y = np.cos(yaw), np.sin(yaw)
            R = np.array([[cos_y, -sin_y], [-sin_y, -cos_y]])
        else:
            R = np.array([[1.0, 0.0], [0.0, -1.0]])

        M = np.eye(3)
        M[:2, :2] = s * R
        M[:2, 2] = mu_world - s * (R @ mu_ref)
        return M

    if len(ref_pts) < 2:
        # Only one connected photo and no analytic GSD available (checked
        # above) -- there is no reliable source left to derive scale or
        # orientation from. A previous version of this code guessed a
        # fixed 0.03 m/px scale here, which still produced a
        # georeferenced-looking GeoTIFF, just one whose scale/position had
        # no real relationship to the actual survey. A result that *looks*
        # valid but isn't is worse than no result, since it's easy to miss
        # -- so this fails loudly instead of writing anything.
        raise UnreliableGeoreferenceError(
            "Not enough information to reliably georeference this photo: only one usable "
            "reference point and no analytic GSD estimate (the photo's EXIF is missing focal "
            "length, image width, or a valid altitude). Refusing to guess a scale, since the "
            "result would not match the real-world layout."
        )

    if is_gcp.any():
        return _fit_similarity_gcp_icp(ref_pts, world_pts, weights, is_gcp, feedback=feedback)

    return _umeyama_2d_weighted(ref_pts, world_pts, weights)


def _make_feather_mask(h, w):
    mask = np.ones((h, w), dtype=np.uint8) * 255
    border = max(2, min(h, w) // 40)
    mask[:border, :] = 0
    mask[-border:, :] = 0
    mask[:, :border] = 0
    mask[:, -border:] = 0
    dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    dist = dist / (dist.max() + 1e-6)
    return dist.astype(np.float32)


def _make_footprint_mask(h, w):
    """Binary (0/255) mask of a photo's own footprint, shrunk a few pixels
    inward from the true edges. Used by seam blending (unlike the
    feather-ramp mask above, this is a hard boolean footprint, not a
    weight). The inward shrink keeps warpPerspective's bilinear
    interpolation at the photo's border -- which blends real pixels with
    the implicit black/out-of-bounds fill just outside the frame -- from
    leaking a thin dark fringe into the composite."""
    border = max(2, min(h, w) // 200)
    mask = np.ones((h, w), dtype=np.uint8) * 255
    mask[:border, :] = 0
    mask[-border:, :] = 0
    mask[:, :border] = 0
    mask[:, -border:] = 0
    return mask


# Above this crop size (in pixels), the graph-cut seam search is run on a
# downscaled copy and the resulting seam mask is upscaled back -- graph-cut
# is a max-flow computation whose cost grows with pixel count, and most
# overlaps between drone photos are much smaller than this, so the
# downscale path only kicks in for unusually large overlaps (e.g. two
# photos that are nearly duplicates of each other).
MAX_SEAM_CROP_PIXELS = 1_500_000

_seam_finder = None


def _get_seam_finder():
    # created lazily & reused: cv2.detail.GraphCutSeamFinder carries some
    # setup cost, and render_mosaic may call this once per overlapping
    # photo pair
    global _seam_finder
    if _seam_finder is None:
        _seam_finder = cv2.detail.GraphCutSeamFinder("COST_COLOR")
    return _seam_finder


def _find_seam_owner_mask(existing_img, new_img, existing_mask, new_mask):
    """Given the already-composited pixels (existing_img/existing_mask) and
    an incoming photo (new_img/new_mask) over the same small crop, find the
    best cut line between them (the one that avoids slicing through areas
    where the two disagree the most, e.g. a shifted rooftop) using graph
    cut. Returns a boolean array (same shape as the crop): True where the
    incoming photo should win.
    """
    h, w = existing_mask.shape
    scale = 1.0
    if h * w > MAX_SEAM_CROP_PIXELS:
        scale = (MAX_SEAM_CROP_PIXELS / (h * w)) ** 0.5
        small_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        existing_img_s = cv2.resize(existing_img, small_size, interpolation=cv2.INTER_AREA)
        new_img_s = cv2.resize(new_img, small_size, interpolation=cv2.INTER_AREA)
        existing_mask_s = cv2.resize(existing_mask.astype(np.uint8) * 255, small_size, interpolation=cv2.INTER_NEAREST)
        new_mask_s = cv2.resize(new_mask.astype(np.uint8) * 255, small_size, interpolation=cv2.INTER_NEAREST)
    else:
        existing_img_s, new_img_s = existing_img, new_img
        existing_mask_s = existing_mask.astype(np.uint8) * 255
        new_mask_s = new_mask.astype(np.uint8) * 255

    finder = _get_seam_finder()
    src = [existing_img_s.astype(np.float32), new_img_s.astype(np.float32)]
    masks_in = [existing_mask_s, new_mask_s]
    result = finder.find(src, [(0, 0), (0, 0)], masks_in)
    new_owner_small = result[1].get() > 0

    if scale != 1.0:
        new_owner = cv2.resize(new_owner_small.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
    else:
        new_owner = new_owner_small
    return new_owner


def compute_exposure_gains(images, connected, min_gain=0.7, max_gain=1.4, feedback=print):
    """Estimate a simple per-photo brightness gain so overlapping photos
    taken under slightly different light (a cloud passing over, auto-
    exposure drift between shots, etc.) don't show an obvious brightness
    'seam' where two photos meet.

    This is a lightweight global compensation, NOT true photometric/vignette
    correction: it equalizes each photo's overall mean luminance to the
    group's median, rather than modelling per-pixel exposure differences
    or fitting gains from the actual overlap regions between neighboring
    photos (the way e.g. OpenCV's detail::ExposureCompensator does). It's
    cheap to compute and helps with the common case (one or two frames
    noticeably brighter/darker than the rest of the flight) while staying
    fast enough to run on every photo unconditionally. The seam-mode
    blending in render_mosaic() already picks cut lines through the areas
    that agree best, which further hides small residual differences.

    Gains are clamped to [min_gain, max_gain] so a genuinely dark/bright
    scene (e.g. a shadow, a reflective roof) doesn't get forced to match
    everything else and blown out or crushed.

    Returns a list of per-photo gain floats (1.0 for any unconnected /
    skipped photo, since those aren't blended into the mosaic anyway).
    """
    means = []
    idx_connected = [i for i in range(len(images)) if connected[i]]
    for i in idx_connected:
        # sample luminance instead of decoding/averaging every pixel --
        # plenty accurate for a global brightness estimate and much
        # cheaper on large photos
        gray = cv2.cvtColor(images[i], cv2.COLOR_BGR2GRAY)
        means.append(float(np.mean(gray[::4, ::4])))

    if len(means) < 2:
        return [1.0] * len(images)

    target = float(np.median(means))
    if target < 1e-3:
        return [1.0] * len(images)

    gains = [1.0] * len(images)
    n_adjusted = 0
    for i, mean_val in zip(idx_connected, means):
        raw_gain = target / max(mean_val, 1e-3)
        gain = float(np.clip(raw_gain, min_gain, max_gain))
        gains[i] = gain
        if abs(gain - 1.0) > 0.03:
            n_adjusted += 1

    if n_adjusted:
        feedback(f"        Adjusted brightness on {n_adjusted}/{len(idx_connected)} photo(s) to reduce exposure seams.")

    return gains


def apply_exposure_gains(images, gains):
    """Apply per-photo brightness gains (see compute_exposure_gains) in
    place-equivalent fashion, returning new arrays -- originals are left
    untouched in case a caller still needs the unadjusted images."""
    out = []
    for img, gain in zip(images, gains):
        if abs(gain - 1.0) < 1e-3:
            out.append(img)
        else:
            out.append(np.clip(img.astype(np.float32) * gain, 0, 255).astype(np.uint8))
    return out


def render_mosaic(images, rel_transforms, connected, world_similarity, gsd=None,
                   max_canvas_dim=30000, max_canvas_bytes=2 * 1024 ** 3, feedback=print,
                   blend_mode="seam", seam_feather_px=12):
    """Warp all (connected) photos onto one shared world canvas & blend the
    overlapping areas.

    blend_mode:
      "seam" (default) -- for each pair of overlapping photos, find a cut
        line with graph-cut seam finding (cv2.detail.GraphCutSeamFinder)
        that runs through the parts where the two photos already agree
        most closely, then hard-assign each pixel to whichever photo lands
        on its side of that line (with only a `seam_feather_px`-wide band
        blended right at the line itself, to hide pixel-level aliasing).
        This avoids the "double-exposed"/ghosted look that plain
        weighted-average blending produces on anything that isn't at
        ground level (rooftops, walls, trees) -- see "feather" below for
        why. It doesn't remove the underlying parallax/perspective error
        on tall objects (that needs true DSM-based orthorectification,
        out of scope here), but replaces ghosting with a cleaner, sharper
        cut through it.
      "feather" -- the previous behaviour: every overlapping photo is
        averaged together, weighted by a distance-to-edge ramp. Simple and
        seam-free on flat ground, but tall objects (roofs, walls) that
        each photo sees in a slightly different pixel position get
        blended into a semi-transparent double image instead of a clean
        cut. Kept as an option for flat/ground-only surveys (farmland,
        open fields) where there's nothing tall to ghost in the first
        place and a fully seamless blend is preferable.

    seam_feather_px: half-width (in pixels) of the blend band straddling
    each seam line in "seam" mode. Only used in "seam" mode.

    positions.

    max_canvas_bytes: soft memory budget (in bytes) for the two accumulator
    buffers (acc_color + acc_weight). If the canvas at the estimated GSD
    would need more than this, gsd is automatically coarsened (the output
    covers the same area at a lower resolution) so the process degrades
    gracefully instead of raising MemoryError / crashing QGIS. Default
    2 GiB is a compromise that should be safe even on modest machines
    while still allowing fairly large survey areas at a normal GSD.
    max_canvas_dim is a secondary hard cap on any single side of the
    canvas (in pixels), independent of the byte budget, as a safety net
    for degenerate/very elongated canvases (e.g. from a bad match).
    """
    n = len(images)
    full_transforms = []
    for i in range(n):
        if not connected[i]:
            full_transforms.append(None)
            continue
        full_transforms.append(world_similarity @ rel_transforms[i])

    # automatically estimate GSD (meters/pixel) from the average scale of each photo, if not set manually
    if gsd is None:
        scales = []
        for i in range(n):
            if not connected[i]:
                continue
            h, w = images[i].shape[:2]
            p0 = np.array([w / 2.0, h / 2.0, 1.0])
            p1 = np.array([w / 2.0 + 50, h / 2.0, 1.0])
            w0 = full_transforms[i] @ p0
            w1 = full_transforms[i] @ p1
            w0 = w0[:2] / w0[2]
            w1 = w1[:2] / w1[2]
            dist_m = np.linalg.norm(w1 - w0)
            scales.append(dist_m / 50.0)
        gsd = float(np.median(scales)) if scales else 0.03

    # compute the world bounding box from all photo corners
    all_world_pts = []
    for i in range(n):
        if not connected[i]:
            continue
        h, w = images[i].shape[:2]
        corners = np.array([[0, 0, 1], [w, 0, 1], [w, h, 1], [0, h, 1]], dtype=np.float64).T
        wc = full_transforms[i] @ corners
        wc = (wc[:2] / wc[2]).T
        all_world_pts.append(wc)
    all_world_pts = np.vstack(all_world_pts)
    minx, miny = all_world_pts.min(axis=0)
    maxx, maxy = all_world_pts.max(axis=0)

    canvas_w = int(np.ceil((maxx - minx) / gsd))
    canvas_h = int(np.ceil((maxy - miny) / gsd))

    # BYTES_PER_PIXEL: worst case is "feather" mode's two accumulator
    # buffers (acc_color: 3 x float32 + acc_weight: 1 x float32); "seam"
    # mode is lighter (uint8 canvas + bool mask) but we budget for the
    # worse case so switching blend_mode never surprises memory use.
    BYTES_PER_PIXEL = 16
    orig_gsd, orig_w, orig_h = gsd, canvas_w, canvas_h

    # guard #1: memory budget for the accumulator buffers (the usual limit
    # in practice -- a wide, flat survey can need this even when neither
    # side alone looks unreasonable)
    needed_bytes = canvas_w * canvas_h * BYTES_PER_PIXEL
    if needed_bytes > max_canvas_bytes:
        factor = (needed_bytes / max_canvas_bytes) ** 0.5
        gsd *= factor
        canvas_w = int(np.ceil((maxx - minx) / gsd))
        canvas_h = int(np.ceil((maxy - miny) / gsd))

    # guard #2: hard cap on any single side, independent of the byte
    # budget (protects against a degenerate/very elongated canvas, e.g.
    # from a bad match, that could pass guard #1 while still being an
    # impractical shape/size)
    if max(canvas_w, canvas_h) > max_canvas_dim:
        factor = max(canvas_w, canvas_h) / max_canvas_dim
        gsd *= factor
        canvas_w = int(np.ceil((maxx - minx) / gsd))
        canvas_h = int(np.ceil((maxy - miny) / gsd))

    if gsd != orig_gsd:
        feedback(
            f"        [WARNING] Output resolution automatically lowered to keep memory use "
            f"reasonable: {orig_w}x{orig_h}px @ {orig_gsd:.4f} m/px -> {canvas_w}x{canvas_h}px @ {gsd:.4f} m/px. "
            f"Set a manual GSD if you need a specific resolution regardless of memory use."
        )

    # world (x,y meters) -> canvas (col,row pixels)
    world_to_canvas = np.array(
        [
            [1.0 / gsd, 0, -minx / gsd],
            [0, -1.0 / gsd, maxy / gsd],
            [0, 0, 1],
        ]
    )

    if blend_mode == "feather":
        acc_color = np.zeros((canvas_h, canvas_w, 3), dtype=np.float32)
        acc_weight = np.zeros((canvas_h, canvas_w), dtype=np.float32)
    elif blend_mode == "seam":
        mosaic_canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
        covered = np.zeros((canvas_h, canvas_w), dtype=bool)
    else:
        raise ValueError(f"Unknown blend_mode: {blend_mode!r} (expected 'seam' or 'feather')")

    # Margin (in pixels) added around each photo's projected footprint
    # before clipping, so bilinear interpolation at the footprint's edge
    # still has real source pixels to draw from instead of being clipped
    # exactly at the geometric boundary.
    ROI_MARGIN_PX = 4

    n_seams_found = 0

    for i in range(n):
        if not connected[i]:
            continue
        h, w = images[i].shape[:2]
        T = world_to_canvas @ full_transforms[i]

        # Warping every photo onto the FULL canvas (as the previous version
        # did) means both the temporary warp buffer and the compute cost
        # scale with the *entire mosaic's* size for *every single photo* --
        # for a flight with hundreds of photos over a large canvas, that's
        # by far the biggest memory/time cost in the whole pipeline, even
        # though each individual photo only ever covers a small fraction of
        # the final canvas. Instead, warp each photo only into its own
        # projected footprint's bounding box, then accumulate into that
        # sub-region of the canvas. This keeps the temporary per-photo
        # buffer small (roughly photo-sized, not canvas-sized) and skips
        # computing pixels that would end up blank anyway.
        corners = np.array([[0, 0, 1], [w, 0, 1], [w, h, 1], [0, h, 1]], dtype=np.float64).T
        cc = T @ corners
        cc = cc[:2] / cc[2]
        x0 = max(0, int(np.floor(cc[0].min())) - ROI_MARGIN_PX)
        x1 = min(canvas_w, int(np.ceil(cc[0].max())) + ROI_MARGIN_PX)
        y0 = max(0, int(np.floor(cc[1].min())) - ROI_MARGIN_PX)
        y1 = min(canvas_h, int(np.ceil(cc[1].max())) + ROI_MARGIN_PX)
        roi_w, roi_h = x1 - x0, y1 - y0
        if roi_w <= 0 or roi_h <= 0:
            continue  # photo's footprint fell entirely outside the canvas (shouldn't normally happen)

        # shift the transform so this photo's footprint lands at (0,0) in
        # the small ROI buffer instead of at its true canvas position
        T_roi = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1]]) @ T

        warped = cv2.warpPerspective(images[i], T_roi, (roi_w, roi_h), flags=cv2.INTER_LINEAR)

        if blend_mode == "feather":
            weight_src = _make_feather_mask(h, w)
            warped_weight = cv2.warpPerspective(weight_src, T_roi, (roi_w, roi_h), flags=cv2.INTER_LINEAR)
            acc_color[y0:y1, x0:x1] += warped.astype(np.float32) * warped_weight[:, :, None]
            acc_weight[y0:y1, x0:x1] += warped_weight
            continue

        # --- blend_mode == "seam" ---
        footprint_src = _make_footprint_mask(h, w)
        new_mask = cv2.warpPerspective(footprint_src, T_roi, (roi_w, roi_h), flags=cv2.INTER_NEAREST) > 0

        comp_roi = mosaic_canvas[y0:y1, x0:x1]
        existing_mask = covered[y0:y1, x0:x1]
        overlap = existing_mask & new_mask

        if not overlap.any():
            # nothing painted here yet (or this photo's footprint doesn't
            # touch it) -- no seam needed, just paste the new photo in
            new_only = new_mask & ~existing_mask
            comp_roi[new_only] = warped[new_only]
        else:
            # Only run graph-cut on the overlap's own bounding box (plus a
            # margin for the feather band), not the whole ROI -- the ROI
            # can be photo-sized while the actual conflicting area is
            # often a much smaller sliver where two flight-path photos
            # meet.
            ys, xs = np.where(overlap)
            m = seam_feather_px + 4
            oy0, oy1 = max(0, ys.min() - m), min(roi_h, ys.max() + m + 1)
            ox0, ox1 = max(0, xs.min() - m), min(roi_w, xs.max() + m + 1)

            crop_existing_mask = existing_mask[oy0:oy1, ox0:ox1]
            crop_new_mask = new_mask[oy0:oy1, ox0:ox1]
            crop_comp = comp_roi[oy0:oy1, ox0:ox1]
            crop_warped = warped[oy0:oy1, ox0:ox1]

            new_owner = _find_seam_owner_mask(crop_comp, crop_warped, crop_existing_mask, crop_new_mask)
            n_seams_found += 1

            union_mask = crop_existing_mask | crop_new_mask
            d_new = cv2.distanceTransform(new_owner.astype(np.uint8), cv2.DIST_L2, 5)
            d_old = cv2.distanceTransform((~new_owner & union_mask).astype(np.uint8), cv2.DIST_L2, 5)
            # signed distance to the seam line: positive on the new
            # photo's side, negative on the existing composite's side,
            # ramping smoothly across a `seam_feather_px`-wide band
            # instead of a hard 1px jump (which would show as a visible
            # jagged edge at full resolution)
            alpha = np.clip(0.5 + (d_new - d_old) / (2.0 * seam_feather_px), 0.0, 1.0).astype(np.float32)

            # pixels only the new photo covers (no existing data to blend
            # against) always take the new photo fully, and vice versa
            crop_new_only = crop_new_mask & ~crop_existing_mask
            crop_old_only = crop_existing_mask & ~crop_new_mask
            alpha[crop_new_only] = 1.0
            alpha[crop_old_only] = 0.0

            touched = crop_existing_mask | crop_new_mask
            crop_comp[touched] = (
                crop_comp[touched].astype(np.float32) * (1 - alpha[touched, None])
                + crop_warped[touched].astype(np.float32) * alpha[touched, None]
            ).astype(np.uint8)
            comp_roi[oy0:oy1, ox0:ox1] = crop_comp

            # paste any part of the new photo's footprint that fell
            # outside the overlap's bounding box (i.e. didn't need a seam)
            new_only_full = new_mask & ~existing_mask
            new_only_full[oy0:oy1, ox0:ox1] = False
            if new_only_full.any():
                comp_roi[new_only_full] = warped[new_only_full]

        covered[y0:y1, x0:x1] |= new_mask

    if blend_mode == "feather":
        mosaic = np.zeros_like(acc_color, dtype=np.uint8)
        valid = acc_weight > 1e-4
        mosaic[valid] = (acc_color[valid] / acc_weight[valid, None]).astype(np.uint8)
        # `valid` already tracks exactly which canvas pixels received any
        # contribution from a warped photo footprint (acc_weight > 0), so
        # it's a true coverage mask -- reuse it directly instead of having
        # the caller re-derive coverage from pixel intensity, which would
        # wrongly mark genuinely dark photo content (deep shadows, black
        # rooftops, etc.) as "outside the mosaic".
        coverage_mask = valid
    else:
        mosaic = mosaic_canvas
        if n_seams_found:
            feedback(f"        Found {n_seams_found} seam(s) between overlapping photos.")
        # `covered` was accumulated above from each photo's actual warped
        # footprint mask (_make_footprint_mask), not from pixel color, so
        # it stays correct even where a photo legitimately contains
        # black/near-black pixels.
        coverage_mask = covered

    # pixel(col,row) -> world(x,y) transform, formatted as (a,b,c / d,e,f) for rasterio's Affine
    world_transform_2x3 = [
        [gsd, 0, minx],
        [0, -gsd, maxy],
    ]

    return mosaic, world_transform_2x3, gsd, coverage_mask
