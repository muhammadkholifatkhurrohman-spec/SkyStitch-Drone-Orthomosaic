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


def _umeyama_2d(src_pts, dst_pts):
    """Find the best 2D similarity transform (uniform scale + rotation, and
    a reflection if the data calls for it, + translation) mapping
    src_pts -> dst_pts (least squares, Umeyama/Procrustes).

    ABOUT THE REFLECTION: `src_pts` live in "reference pixel space" (row
    increases DOWNWARDS -- normal image/screen convention), while
    `dst_pts` are real-world coordinates from pyproj/UTM (Y increases
    NORTHWARDS -- normal map convention). Those two conventions have
    opposite handedness, so the correct transform between them always
    includes exactly one reflection (a mirrored axis) in addition to any
    rotation -- it's a structural fact about the two coordinate systems,
    not something that depends on the particular GPS points.

    The previous version of this function forced the fit to be a pure
    rotation (`R = U @ S @ Vt` with `S` flipped specifically to CANCEL OUT
    any reflection). That's the textbook Umeyama/Kabsch behaviour, correct
    when both point sets share the same handedness -- but wrong here,
    since it actively fights the reflection the data needs. That's what
    caused the whole orthomosaic to come out rotated/mirrored relative to
    its true position and orientation on the map (it landed in roughly the
    right place, but tilted at the wrong angle). Letting the natural,
    unconstrained least-squares solution stand (which is allowed to be a
    reflection) fixes it -- and matches the two GPS-fallback branches below,
    which already deliberately negate the Y scale (`M[1, 1] = -s`) for the
    exact same reason.
    """
    src = np.asarray(src_pts, dtype=np.float64)
    dst = np.asarray(dst_pts, dtype=np.float64)
    n = src.shape[0]

    mu_src = src.mean(axis=0)
    mu_dst = dst.mean(axis=0)
    src_c = src - mu_src
    dst_c = dst - mu_dst

    var_src = (src_c ** 2).sum() / n
    cov = (dst_c.T @ src_c) / n

    U, D, Vt = np.linalg.svd(cov)
    R = U @ Vt  # best-fit orthogonal matrix -- rotation OR reflection, whichever the data needs
    scale = D.sum() / var_src if var_src > 1e-9 else 1.0
    t = mu_dst - scale * (R @ mu_src)

    M = np.eye(3)
    M[:2, :2] = scale * R
    M[:2, 2] = t
    return M


def fit_world_similarity(images, rel_transforms, connected, world_xy, analytic_gsd=None, feedback=print):
    """Compute the transform from 'reference pixel space' -> real-world
    coordinates (meters). If the GPS baseline between photos is too tight
    (< 8m, below consumer GPS accuracy), the scale is forced to use the
    analytic estimate from camera parameters, and GPS is only used to
    determine position (translation)."""
    ref_pts = []
    world_pts = []
    for i, img in enumerate(images):
        if not connected[i]:
            continue
        h, w = img.shape[:2]
        center = np.array([w / 2.0, h / 2.0, 1.0])
        ref_center = rel_transforms[i] @ center
        ref_center = ref_center[:2] / ref_center[2]
        ref_pts.append(ref_center)
        world_pts.append(world_xy[i])

    ref_pts = np.array(ref_pts)
    world_pts = np.array(world_pts)

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
        M = np.eye(3)
        M[0, 0] = s
        M[1, 1] = -s
        M[0, 2] = mu_world[0] - s * mu_ref[0]
        M[1, 2] = mu_world[1] + s * mu_ref[1]
        return M

    if len(ref_pts) < 2:
        M = np.eye(3)
        M[0, 2] = world_pts[0][0] - ref_pts[0][0] * 0.03
        M[1, 2] = world_pts[0][1] + ref_pts[0][1] * 0.03
        M[0, 0] = 0.03
        M[1, 1] = -0.03
        return M

    return _umeyama_2d(ref_pts, world_pts)


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


def render_mosaic(images, rel_transforms, connected, world_similarity, gsd=None, max_canvas_dim=18000):
    """Warp all (connected) photos onto one shared world canvas & blend the overlapping areas."""
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

    # guard against the canvas size exploding (e.g. due to a bad match)
    if max(canvas_w, canvas_h) > max_canvas_dim:
        factor = max(canvas_w, canvas_h) / max_canvas_dim
        gsd *= factor
        canvas_w = int(np.ceil((maxx - minx) / gsd))
        canvas_h = int(np.ceil((maxy - miny) / gsd))

    # world (x,y meters) -> canvas (col,row pixels)
    world_to_canvas = np.array(
        [
            [1.0 / gsd, 0, -minx / gsd],
            [0, -1.0 / gsd, maxy / gsd],
            [0, 0, 1],
        ]
    )

    acc_color = np.zeros((canvas_h, canvas_w, 3), dtype=np.float32)
    acc_weight = np.zeros((canvas_h, canvas_w), dtype=np.float32)

    for i in range(n):
        if not connected[i]:
            continue
        h, w = images[i].shape[:2]
        T = world_to_canvas @ full_transforms[i]

        warped = cv2.warpPerspective(images[i], T, (canvas_w, canvas_h), flags=cv2.INTER_LINEAR)
        weight_src = _make_feather_mask(h, w)
        warped_weight = cv2.warpPerspective(weight_src, T, (canvas_w, canvas_h), flags=cv2.INTER_LINEAR)

        acc_color += warped.astype(np.float32) * warped_weight[:, :, None]
        acc_weight += warped_weight

    mosaic = np.zeros_like(acc_color, dtype=np.uint8)
    valid = acc_weight > 1e-4
    mosaic[valid] = (acc_color[valid] / acc_weight[valid, None]).astype(np.uint8)

    # pixel(col,row) -> world(x,y) transform, formatted as (a,b,c / d,e,f) for rasterio's Affine
    world_transform_2x3 = [
        [gsd, 0, minx],
        [0, -gsd, maxy],
    ]

    return mosaic, world_transform_2x3, gsd
