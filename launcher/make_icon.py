"""アプリのアイコン（static/app.ico）を生成する。

UI左上のロゴ（水色の角丸＋白い再生マーク）と同じ意匠。
build.cmd から呼ばれる。アイコンを変えたいときはここを直して build.cmd を再実行する。
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static" / "app.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]
BRAND = (41, 182, 246, 255)   # 水色。index.html の --brand と同じ色（YouTube の赤と区別する）
SS = 4                         # 縁を滑らかにするための超解像倍率


def draw(size):
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 横長の角丸（YouTubeロゴ風）。小さいサイズでは潰れないよう余白を詰める
    pad_x = s * (0.04 if size <= 24 else 0.06)
    h = s * (0.74 if size <= 24 else 0.70)
    top = (s - h) / 2
    d.rounded_rectangle([pad_x, top, s - pad_x, top + h],
                        radius=h * 0.28, fill=BRAND)
    # 再生マーク（重心が中央に来るよう少し右へ寄せる）
    tw = h * 0.40
    th = h * 0.46
    cx = s / 2 + tw * 0.12
    cy = s / 2
    d.polygon([(cx - tw / 2, cy - th / 2), (cx - tw / 2, cy + th / 2),
               (cx + tw / 2, cy)], fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    images = [draw(n) for n in SIZES]
    images[-1].save(OUT, format="ICO", sizes=[(n, n) for n in SIZES],
                    append_images=images[:-1])
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
