from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch


# Source: Design report, Table 10 (paired transitions) and Table 9 (paired totals).
# The side summaries are derived from the four Table 10 cells; no raw observations
# or statistical estimates are invented in this visualization.
N = 64
BOTH_PASS = 7
NEW_ONLY_PASS = 55
REFERENCE_ONLY_PASS = 0
BOTH_FAIL = 2

assert BOTH_PASS + NEW_ONLY_PASS + REFERENCE_ONLY_PASS + BOTH_FAIL == N

NEW_PASS = BOTH_PASS + NEW_ONLY_PASS
REFERENCE_PASS = BOTH_PASS + REFERENCE_ONLY_PASS
NEW_RATE = NEW_PASS / N * 100
REFERENCE_RATE = REFERENCE_PASS / N * 100
PAIRED_COUNT_DIFF = NEW_ONLY_PASS - REFERENCE_ONLY_PASS
PAIRED_PP_DIFF = NEW_RATE - REFERENCE_RATE

COLORS = {
    "white": "#FFFFFF",
    "navy": "#254BA6",
    "blue": "#315BC7",
    "blue_light": "#EAF0FC",
    "blue_edge": "#B9C9EE",
    "teal": "#168B82",
    "teal_light": "#E6F4F1",
    "teal_edge": "#AFDCD4",
    "rose": "#A65C78",
    "rose_light": "#F8EDF2",
    "rose_edge": "#E8C8D5",
    "gray": "#53677C",
    "gray_light": "#F1F4F7",
    "gray_edge": "#D6DFE8",
    "text": "#24364B",
    "muted": "#6A7D91",
    "line": "#DCE4ED",
    "track": "#E8EDF3",
}


def add_box(
    ax: plt.Axes,
    x: float,
    y: float,
    width: float,
    height: float,
    face: str,
    edge: str = "none",
    linewidth: float = 0.8,
    radius: float = 12,
) -> None:
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            width,
            height,
            boxstyle=f"round,pad=0,rounding_size={radius}",
            linewidth=linewidth,
            edgecolor=edge,
            facecolor=face,
        )
    )


def add_text(
    ax: plt.Axes,
    x: float,
    y: float,
    text: str,
    size: float,
    color: str,
    weight: str = "normal",
    ha: str = "left",
    va: str = "center",
) -> None:
    ax.text(
        x,
        y,
        text,
        fontsize=size,
        color=color,
        fontweight=weight,
        ha=ha,
        va=va,
        family="Microsoft YaHei",
    )


def add_matrix_cell(
    ax: plt.Axes,
    x: float,
    y: float,
    count: int,
    label: str,
    face: str,
    edge: str,
    number_color: str,
) -> None:
    add_box(ax, x, y, 300, 142, face, edge=edge, linewidth=1.0, radius=13)
    add_text(ax, x + 150, y + 99, f"{count}", 20 if count != NEW_ONLY_PASS else 22,
             number_color, weight="bold", ha="center")
    add_text(ax, x + 150, y + 35, label, 8.1, COLORS["text"], weight="medium", ha="center")


def add_metric_card(
    ax: plt.Axes,
    y: float,
    label: str,
    passed: int,
    rate: float,
    color: str,
) -> None:
    x = 1055
    width = 380
    height = 157
    add_box(ax, x, y, width, height, COLORS["white"], edge=COLORS["line"], linewidth=1.0, radius=12)
    add_text(ax, x + 22, y + 132, label, 7.7, color, weight="bold")
    add_text(ax, x + 22, y + 82, f"{passed}/{N}", 18, COLORS["text"], weight="bold")
    add_text(ax, x + width - 22, y + 83, f"{rate:.1f}%", 9.5, color, weight="bold", ha="right")
    track_x, track_y, track_w, track_h = x + 22, y + 28, width - 44, 8
    add_box(ax, track_x, track_y, track_w, track_h, COLORS["track"], radius=6)
    add_box(ax, track_x, track_y, track_w * rate / 100, track_h, color, radius=6)


def build_figure() -> tuple[plt.Figure, plt.Axes]:
    plt.rcParams.update(
        {
            "font.family": "Microsoft YaHei",
            "axes.unicode_minus": False,
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )

    fig, ax = plt.subplots(figsize=(15.2 / 2.54, 9.0 / 2.54), dpi=300)
    fig.patch.set_facecolor(COLORS["white"])
    ax.set_facecolor(COLORS["white"])
    ax.set_xlim(0, 1500)
    ax.set_ylim(0, 900)
    ax.axis("off")

    # Header: one message, scope and denominator are explicit.
    add_text(ax, 70, 842, "CS18 v0.6  /  表 10", 7.5, COLORS["blue"], weight="bold")
    add_text(ax, 70, 798, "严格通过的逐题配对结果", 14, COLORS["text"], weight="bold")
    add_text(
        ax,
        70,
        750,
        "64 道合成业务题｜同一题分别运行新事实核验流程与匹配参考流程",
        8.2,
        COLORS["muted"],
    )
    add_box(ax, 1300, 814, 135, 40, COLORS["blue_light"], radius=18)
    add_text(ax, 1367.5, 834, "配对样本  n=64", 7.2, COLORS["navy"], weight="bold", ha="center")

    # Paired transition matrix (rows: matched reference; columns: new flow).
    add_text(ax, 70, 699, "逐题结果转移（题数）", 9, COLORS["text"], weight="bold")
    add_text(ax, 667, 696, "新事实核验流程判定", 7.5, COLORS["muted"], weight="bold", ha="center")
    add_text(ax, 82, 655, "匹配参考流程", 7.2, COLORS["muted"], weight="bold")

    x_left, x_right = 350, 665
    col_y = 615
    for x, label in ((x_left, "通过"), (x_right, "未通过")):
        add_box(ax, x, col_y, 300, 40, COLORS["blue_light"], radius=9)
        add_text(ax, x + 150, col_y + 20, label, 7.8, COLORS["navy"], weight="bold", ha="center")

    row_top_y, row_bottom_y = 453, 291
    for y, label, color, fill in (
        (row_top_y, "通过", COLORS["navy"], COLORS["blue_light"]),
        (row_bottom_y, "未通过", COLORS["gray"], COLORS["gray_light"]),
    ):
        add_box(ax, 230, y + 51, 94, 40, fill, radius=18)
        add_text(ax, 277, y + 71, label, 7.8, color, weight="bold", ha="center")

    add_matrix_cell(ax, x_left, row_top_y, BOTH_PASS, "两流程均通过", COLORS["blue_light"], COLORS["blue_edge"], COLORS["navy"])
    add_matrix_cell(ax, x_right, row_top_y, REFERENCE_ONLY_PASS, "仅参考流程通过", COLORS["rose_light"], COLORS["rose_edge"], COLORS["rose"])
    add_matrix_cell(ax, x_left, row_bottom_y, NEW_ONLY_PASS, "仅新流程通过", COLORS["teal_light"], COLORS["teal_edge"], COLORS["teal"])
    add_matrix_cell(ax, x_right, row_bottom_y, BOTH_FAIL, "两流程均未通过", COLORS["gray_light"], COLORS["gray_edge"], COLORS["gray"])

    # Derived marginal totals make the matrix immediately scannable.
    add_metric_card(ax, 480, "新事实核验流程严格通过", NEW_PASS, NEW_RATE, COLORS["teal"])
    add_metric_card(ax, 303, "匹配参考流程严格通过", REFERENCE_PASS, REFERENCE_RATE, COLORS["blue"])

    # Paired difference is kept distinct from a general accuracy claim.
    add_box(ax, 1055, 138, 380, 140, COLORS["teal_light"], edge=COLORS["teal_edge"], linewidth=1.0, radius=12)
    add_text(ax, 1078, 248, "配对通过差", 7.7, COLORS["teal"], weight="bold")
    add_text(ax, 1078, 210, f"+{PAIRED_COUNT_DIFF} 题", 13, COLORS["text"], weight="bold")
    add_text(ax, 1078, 170, f"+{PAIRED_PP_DIFF:.2f} 个百分点", 8, COLORS["teal"], weight="bold")

    # Evidence definition and limitations remain visible on the figure.
    ax.plot([70, 1435], [123, 123], color=COLORS["line"], linewidth=0.8)
    add_text(ax, 70, 94, "严格通过：人工评定为“正确”且证据为“充分”。", 7, COLORS["text"])
    add_text(
        ax,
        70,
        65,
        "边界：合成题上的描述性配对对照，不代表真实业务准确率；评审顺序未独立随机化等限制见正文。",
        6.8,
        COLORS["muted"],
    )

    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    return fig, ax


def main() -> None:
    out_dir = Path(__file__).resolve().parent
    stem = "表10_CS18配对结果矩阵_视觉试稿_v0.1"
    fig, _ = build_figure()
    fig.savefig(out_dir / f"{stem}.svg", format="svg", facecolor=COLORS["white"])
    fig.savefig(out_dir / f"{stem}.png", format="png", dpi=300, facecolor=COLORS["white"])
    plt.close(fig)
    print(f"created {out_dir / f'{stem}.svg'}")
    print(f"created {out_dir / f'{stem}.png'}")
    print(
        f"validated n={N}, new={NEW_PASS}/{N} ({NEW_RATE:.1f}%), "
        f"reference={REFERENCE_PASS}/{N} ({REFERENCE_RATE:.1f}%), "
        f"paired difference=+{PAIRED_COUNT_DIFF} cases / +{PAIRED_PP_DIFF:.2f} pp"
    )


if __name__ == "__main__":
    main()
