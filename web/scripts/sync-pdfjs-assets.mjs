import { cp, mkdir, stat } from 'node:fs/promises'
import { join } from 'node:path'

const source = join(process.cwd(), 'node_modules', 'pdfjs-dist')
const target = join(process.cwd(), 'public', 'pdfjs')

for (const directory of ['cmaps', 'standard_fonts']) {
  const from = join(source, directory)
  const to = join(target, directory)
  try {
    await stat(from)
    await mkdir(target, { recursive: true })
    await cp(from, to, { recursive: true, force: true })
  } catch {
    // The worker is bundled by Vite. These optional assets are copied when present.
  }
}
