"""Windows 无 LibreOffice 时复用 canonical render_docx 的 PNG 流程。

仅替换文档转换入口：使用与 Word 同版导出的 PDF，不修改管理目录的渲染器。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument("renderer")
    parser.add_argument("docx")
    parser.add_argument("output_dir")
    args=parser.parse_args()
    renderer_path=Path(args.renderer).resolve()
    spec=importlib.util.spec_from_file_location("canonical_docx_renderer",renderer_path)
    if spec is None or spec.loader is None:raise RuntimeError("Cannot load canonical renderer")
    renderer=importlib.util.module_from_spec(spec);spec.loader.exec_module(renderer)
    document=Path(args.docx).resolve()
    pdf=document.with_suffix(".pdf")
    if not pdf.is_file():raise FileNotFoundError(pdf)
    # canonical rasterize 仍负责 Poppler 渲染、页面编号、图片输出。
    renderer.convert_to_pdf=lambda *_a,**_kw:(str(pdf),"Native Word PDF export from the same DOCX")
    output=Path(args.output_dir).resolve()
    dpi=renderer.calc_dpi_via_ooxml_docx(str(document),1600,2000)
    pages=renderer.rasterize(str(document),str(output),dpi,False,False)
    print(json.dumps({"document":document.name,"pages":len(pages),"dpi":dpi,"renderer":str(renderer_path)},ensure_ascii=False))


if __name__=="__main__":main()
