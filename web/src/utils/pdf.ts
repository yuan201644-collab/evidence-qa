import { GlobalWorkerOptions, getDocument } from 'pdfjs-dist'
import PdfWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

GlobalWorkerOptions.workerSrc = PdfWorker

const base = import.meta.env.BASE_URL
export const pdfLoadOptions = {
  cMapUrl: `${base}pdfjs/cmaps/`,
  cMapPacked: true,
  standardFontDataUrl: `${base}pdfjs/standard_fonts/`,
}

export { getDocument }

export type CachedPage = {
  canvas: HTMLCanvasElement
  viewport: { width: number; height: number; transform: number[] }
  pageView: number[]
  pageCount: number
}

const maxCachedPages = 5
const cache = new Map<string, CachedPage>()

export function getCachedPage(key: string) {
  const item = cache.get(key)
  if (!item) return undefined
  cache.delete(key)
  cache.set(key, item)
  return item
}

export function setCachedPage(key: string, item: CachedPage) {
  cache.delete(key)
  cache.set(key, item)
  if (cache.size > maxCachedPages) cache.delete(cache.keys().next().value as string)
}
