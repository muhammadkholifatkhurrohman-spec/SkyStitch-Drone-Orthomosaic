"""
feature_matching.py
====================
Pipeline stage 1: detect unique feature points in each photo, then match
them between overlapping photos. This is the basis of a simple
"Structure-from-Motion": from the matched points, we can compute how one
photo needs to be rotated/scaled/shifted to align with the others.
"""

import os
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

DETECT_MAX_DIM = 1600  # resize before feature detection for speed (results are rescaled back to original size)


def detect_features(gray_full):
    """Detect SIFT keypoints & descriptors. Detection is done on a
    downscaled version for speed, then keypoint coordinates are rescaled
    back to the original full-resolution photo."""
    h, w = gray_full.shape
    scale = min(1.0, DETECT_MAX_DIM / max(h, w))
    if scale < 1.0:
        small = cv2.resize(gray_full, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    else:
        small = gray_full

    sift = cv2.SIFT_create(nfeatures=6000)
    kp, desc = sift.detectAndCompute(small, None)

    # rescale keypoint coordinates back to original photo scale
    inv_scale = 1.0 / scale
    pts_full = np.array([[k.pt[0] * inv_scale, k.pt[1] * inv_scale] for k in kp], dtype=np.float32)
    return pts_full, desc


def match_pair(pts1, desc1, pts2, desc2, ratio=0.75, min_matches=25):
    """Match two descriptor sets (KNN + Lowe's ratio test), then compute a
    homography with RANSAC to discard incorrect pairs (outliers).
    Returns (H, n_inliers) or (None, 0) if there are too few/weak matches.
    H maps pixel coordinates from photo-2 -> pixel coordinates of photo-1."""
    if desc1 is None or desc2 is None or len(desc1) < 4 or len(desc2) < 4:
        return None, 0

    bf = cv2.BFMatcher(cv2.NORM_L2)
    raw_matches = bf.knnMatch(desc1, desc2, k=2)

    good = []
    for m_n in raw_matches:
        if len(m_n) != 2:
            continue
        m, n = m_n
        if m.distance < ratio * n.distance:
            good.append(m)

    if len(good) < min_matches:
        return None, 0

    src_pts = np.float32([pts2[m.trainIdx] for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32([pts1[m.queryIdx] for m in good])

    H, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, ransacReprojThreshold=5.0)
    if H is None:
        return None, 0

    n_inliers = int(mask.sum()) if mask is not None else 0
    if n_inliers < min_matches:
        return None, 0

    return H, n_inliers


def _candidate_pairs(n, positions, k_neighbors):
    """Return the set of (i, j) pairs (i < j) worth trying to match,
    restricted to each photo's k nearest neighbors by real-world (GPS)
    distance, instead of every possible pair.

    positions: list/array of (x, y) world coordinates (meters), same
    order/length as the photos, or None to fall back to all pairs.
    Falls back to all pairs whenever there aren't enough photos for the
    restriction to be worth it, or when positions aren't available.
    """
    if positions is None or n <= k_neighbors + 1:
        return {(i, j) for i in range(n) for j in range(i + 1, n)}

    pos = np.asarray(positions, dtype=np.float64)

    try:
        from scipy.spatial import cKDTree

        tree = cKDTree(pos)
        # +1 because a point is always its own nearest neighbor
        _, idx = tree.query(pos, k=min(k_neighbors + 1, n))
    except Exception:
        # scipy not available for some reason -- brute-force k-NN instead
        d = np.linalg.norm(pos[:, None, :] - pos[None, :, :], axis=-1)
        idx = np.argsort(d, axis=1)[:, : min(k_neighbors + 1, n)]

    pairs = set()
    for i in range(n):
        for j in idx[i]:
            j = int(j)
            if j == i:
                continue
            pairs.add((i, j) if i < j else (j, i))
    return pairs


def build_match_graph(
    images_gray, positions=None, k_neighbors=12, feedback=print, check_cancel=lambda: None, max_workers=None
):
    """Detect features in all photos, then try to match candidate pairs of
    photos. Returns:
      features: list of (pts, desc) per photo
      edges: dict {(i, j): (H_ij, n_inliers)} for pairs that were
             successfully matched (H_ij maps pixels of photo j -> pixels of photo i)

    positions: real-world (GPS-derived, meters) coordinate per photo, used
    to only try matching each photo against its `k_neighbors` closest
    neighbors instead of every other photo. This turns matching from
    O(n^2) into roughly O(n * k_neighbors), which matters a lot once a
    flight has hundreds of photos -- most of those pairs could never
    overlap anyway. Pass positions=None to match every pair (used for the
    CLI / callers that don't have GPS positions handy).

    check_cancel: called periodically (both while detecting features and
    while matching pairs) so a long-running batch can be canceled promptly
    instead of only checked once before/after the whole function runs.

    max_workers: number of worker threads used to detect features in
    parallel (defaults to min(32, os.cpu_count() + 4), Python's usual
    ThreadPoolExecutor default). Detection of one photo doesn't depend on
    any other, so this is embarrassingly parallel and speeds up Step 2 a
    lot once there are hundreds of photos.

    THREADS, NOT PROCESSES: this used to run on a ProcessPoolExecutor.
    That's the more obvious choice for CPU-bound work in a plain script,
    but it's actively dangerous when this code runs as a QGIS plugin: a
    process pool spawns brand-new OS processes, and inside an app that
    embeds its own Python interpreter (like QGIS), the new process's entry
    point is the QGIS executable itself -- not this script -- so every
    worker "process" was actually relaunching a whole new QGIS window,
    several of which then crashed with "process...terminated abruptly"
    once the pool tried to use them as plain workers. A thread pool never
    spawns a new process, so this can't happen. It still gets real
    parallelism for this workload because cv2's compute-heavy C++ calls
    (including SIFT's detectAndCompute) release Python's GIL while they
    run, the same way numpy's C loops do -- multiple threads' detection
    calls can run concurrently on separate cores instead of queueing
    behind the GIL like pure-Python code would. Matching (below) stays
    sequential since candidate pairs are comparatively cheap and few.
    """
    n = len(images_gray)
    feedback(f"[INFO] Detecting unique features in {n} photos...")
    features = [None] * n
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {executor.submit(detect_features, gray): idx for idx, gray in enumerate(images_gray)}
        try:
            for future in as_completed(future_to_idx):
                check_cancel()
                idx = future_to_idx[future]
                pts, desc = future.result()
                features[idx] = (pts, desc)
                feedback(f"        Photo {idx}: {len(pts)} features detected")
        except BaseException:
            # cancel any not-yet-started work so a cancellation / error
            # doesn't have to wait for every already-queued photo to finish
            # detecting first
            for f in future_to_idx:
                f.cancel()
            raise


    candidate_pairs = _candidate_pairs(n, positions, k_neighbors)
    total_possible = n * (n - 1) // 2
    if positions is not None and len(candidate_pairs) < total_possible:
        feedback(
            f"[INFO] Matching {len(candidate_pairs)} nearby photo pairs "
            f"(out of {total_possible} possible pairs, using GPS position to skip pairs too far apart)..."
        )
    else:
        feedback("[INFO] Matching features between photo pairs...")

    edges = {}
    for i, j in sorted(candidate_pairs):
        check_cancel()
        H, n_inl = match_pair(features[i][0], features[i][1], features[j][0], features[j][1])
        if H is not None:
            edges[(i, j)] = (H, n_inl)
            feedback(f"        Photo {i} <-> Photo {j}: matched ({n_inl} matching points)")
    if not edges:
        feedback("        No photo pairs could be matched.")
    return features, edges
