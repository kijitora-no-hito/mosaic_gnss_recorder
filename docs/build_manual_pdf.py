"""docs/manual.md から PDF 版(docs/manual.pdf)を作る。

使い方: python docs/build_manual_pdf.py
markdown(Python パッケージ)で HTML にし、Qt WebEngine(Chromium)で A4 の PDF に印刷する。
"""
import os
import sys

import markdown
from PySide6.QtCore import QMarginsF, QUrl
from PySide6.QtGui import QPageLayout, QPageSize
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import QApplication

HERE = os.path.dirname(os.path.abspath(__file__))
STYLE = """
body { font-family: 'Yu Gothic UI', 'Meiryo', sans-serif; font-size: 10pt; line-height: 1.6; }
h1 { font-size: 18pt; border-bottom: 2px solid #2471a3; }
h2 { font-size: 14pt; border-bottom: 1px solid #aaa; margin-top: 24px; break-after: avoid; }
h3, h4 { break-after: avoid; }
table { border-collapse: collapse; margin: 6px 0; }
th, td { border: 1px solid #999; padding: 3px 6px; vertical-align: top; }
th { background: #eaeef2; }
code { font-family: Consolas, monospace; background: #f3f3f3; padding: 0 2px; }
img { max-width: 100%; border: 1px solid #ccc; break-inside: avoid; }
p:has(img) { break-inside: avoid; }
"""


def to_html(text, title):
    """markdown を印刷用の HTML にする。"""
    body = markdown.markdown(text, extensions=["tables"])
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>'
            f"<style>{STYLE}</style></head><body>{body}</body></html>")


def build(source, output):
    """source の markdown を output の PDF にする。終わるまでイベントループを回す。"""
    app = QApplication.instance() or QApplication(sys.argv)
    with open(source, encoding="utf-8") as fin:
        html = to_html(fin.read(), "mosaic_gnss_recorder 操作マニュアル")
    page = QWebEnginePage()
    layout = QPageLayout(QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait,
                         QMarginsF(15, 15, 15, 15), QPageLayout.Unit.Millimeter)
    result = {}

    def on_loaded(ok):
        if not ok:
            result["error"] = "HTML を読み込めません"
            app.quit()
            return
        page.printToPdf(os.path.abspath(output), layout)

    def on_printed(path, ok):
        result["ok"] = ok and path
        app.quit()

    page.loadFinished.connect(on_loaded)  # pylint: disable=no-member
    page.pdfPrintingFinished.connect(on_printed)  # pylint: disable=no-member
    base = QUrl.fromLocalFile(os.path.dirname(os.path.abspath(source)) + os.sep)
    page.setHtml(html, base)
    app.exec()
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "PDF を作れません"))


def main():
    """docs/manual.md を docs/manual.pdf にする。"""
    build(os.path.join(HERE, "manual.md"), os.path.join(HERE, "manual.pdf"))
    print(os.path.join(HERE, "manual.pdf"))


if __name__ == "__main__":
    main()
