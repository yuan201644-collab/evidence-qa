import type { Bbox } from '../types/contracts'

export type PdfViewport = { transform: number[] }

/**
 * Converts a PyMuPDF top-left bbox into canvas CSS pixels through pdf.js's
 * viewport transform. `pageView` is pdf.js page.view (the CropBox in PDF
 * user space), so no proportional-width shortcut is used.
 */
export function pdfRectToCanvas(viewport: PdfViewport, pageView: number[], bbox: Bbox) {
  const [vx0, vy0, , vy1] = pageView
  const cropHeight = vy1 - vy0
  const [x0, y0, x1, y1] = bbox
  const ux0 = vx0 + x0
  const ux1 = vx0 + x1
  const uyHigh = vy0 + cropHeight - y0
  const uyLow = vy0 + cropHeight - y1
  const [a, b, c, d, e, f] = viewport.transform
  const point = (x: number, y: number) => ({ x: a * x + c * y + e, y: b * x + d * y + f })
  const first = point(ux0, uyHigh)
  const second = point(ux1, uyLow)
  return {
    left: Math.min(first.x, second.x),
    top: Math.min(first.y, second.y),
    width: Math.abs(second.x - first.x),
    height: Math.abs(second.y - first.y),
  }
}
