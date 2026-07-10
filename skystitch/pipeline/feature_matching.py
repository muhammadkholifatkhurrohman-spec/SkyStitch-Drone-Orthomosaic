"""
feature_matching.py
====================
Pipeline stage 1: detect unique feature points in each photo, then match
them between overlapping photos. This is the basis of a simple
"Structure-from-Motion": from the matched points, we can compute how one
photo needs to be rotated/scaled/shifted to align with the others.
"""

import cv2
import numpy as np

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


def build_match_graph(images_gray, feedback=print, check_cancel=lambda: None):
    """Detect features in all photos, then try to match every pair of photos.
    Returns:
      features: list of (pts, desc) per photo
      edges: dict {(i, j): (H_ij, n_inliers)} for pairs that were
             successfully matched (H_ij maps pixels of photo j -> pixels of photo i)

    check_cancel: called periodically (both while detecting features and
    while matching pairs) so a long-running batch can be canceled promptly
    instead of only checked once before/after the whole function runs.
    """
    n = len(images_gray)
    feedback(f"[INFO] Detecting unique features in {n} photos...")
    features = []
    for idx, gray in enumerate(images_gray):
        check_cancel()
        pts, desc = detect_features(gray)
        features.append((pts, desc))
        feedback(f"        Photo {idx}: {len(pts)} features detected")

    edges = {}
    feedback("[INFO] Matching features between photo pairs...")
    for i in range(n):
        for j in range(i + 1, n):
            check_cancel()
            H, n_inl = match_pair(features[i][0], features[i][1], features[j][0], features[j][1])
            if H is not None:
                edges[(i, j)] = (H, n_inl)
                feedback(f"        Photo {i} <-> Photo {j}: matched ({n_inl} matching points)")
    if not edges:
        feedback("        No photo pairs could be matched.")
    return features, edges
