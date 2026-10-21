"""Screen-to-camera capture model used for the stream and paper measurements.

A QR image is placed in a 1280x720 (or given) camera frame with random
rotation + perspective, display contrast loss, vignetting, optical blur,
optional motion blur, sensor noise and JPEG compression. Parameters are
deliberately pessimistic-to-typical; real cameras were NOT available here.
"""
import cv2
import numpy as np

PROFILES = {
    # name: (rot_deg, persp, blur_sigma, motion_px, noise_sigma, black, white, vignette, jpeg_q)
    "good":    dict(rot=4,  persp=0.02, blur=0.7, motion=0, noise=3,  black=30, white=225, vig=0.10, jpeg=85),
    "typical": dict(rot=10, persp=0.05, blur=1.1, motion=2, noise=6,  black=45, white=200, vig=0.20, jpeg=75),
    "poor":    dict(rot=15, persp=0.08, blur=1.6, motion=3, noise=10, black=60, white=180, vig=0.30, jpeg=60),
}


def capture(qr_img, rng, profile="typical", frame=(1280, 720), qr_frac=0.66):
    """qr_img: PIL L image of the code incl. quiet zone. Returns uint8 HxW array."""
    p = PROFILES[profile] if isinstance(profile, str) else profile
    W, H = frame
    src = np.asarray(qr_img, dtype=np.float32)
    s = src.shape[0]
    side = qr_frac * H
    cx = W / 2 + rng.uniform(-0.05, 0.05) * W
    cy = H / 2 + rng.uniform(-0.03, 0.03) * H
    ang = np.deg2rad(rng.uniform(-p["rot"], p["rot"]))
    corners = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]], dtype=np.float32) * side / 2
    rot = np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]], dtype=np.float32)
    dst = corners @ rot.T + np.array([cx, cy], dtype=np.float32)
    dst += rng.uniform(-p["persp"], p["persp"], size=(4, 2)).astype(np.float32) * side
    srcpts = np.array([[0, 0], [s, 0], [s, s], [0, s]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(srcpts, dst.astype(np.float32))
    # screen: white background around the code (the sender page is white)
    img = cv2.warpPerspective(src, M, (W, H), flags=cv2.INTER_AREA, borderValue=255.0)
    # display contrast
    img = p["black"] + (p["white"] - p["black"]) * (img / 255.0)
    # vignetting / uneven lighting
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    gx, gy = rng.uniform(-1, 1, 2)
    r2 = ((xx - W / 2) / W) ** 2 + ((yy - H / 2) / H) ** 2
    img *= (1 - p["vig"] * r2 * 2) * (1 + 0.08 * (gx * (xx / W - 0.5) + gy * (yy / H - 0.5)))
    if p["blur"] > 0:
        img = cv2.GaussianBlur(img, (0, 0), p["blur"])
    if p["motion"] > 0:
        L = int(rng.integers(0, p["motion"] + 1))
        if L > 1:
            k = np.zeros((L, L), np.float32)
            k[L // 2, :] = 1.0 / L
            Mr = cv2.getRotationMatrix2D((L / 2 - 0.5, L / 2 - 0.5), rng.uniform(0, 180), 1)
            k = cv2.warpAffine(k, Mr, (L, L))
            k /= max(k.sum(), 1e-6)
            img = cv2.filter2D(img, -1, k)
    img += rng.normal(0, p["noise"], img.shape).astype(np.float32)
    img = np.clip(img, 0, 255).astype(np.uint8)
    if p["jpeg"]:
        ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, p["jpeg"]])
        img = cv2.imdecode(enc, cv2.IMREAD_GRAYSCALE)
    return img
