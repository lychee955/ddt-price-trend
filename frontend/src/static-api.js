// The same UI consumes local API responses or an immutable public export.
const compareText = (a, b) => a < b ? -1 : a > b ? 1 : 0

function like(pattern, value) {
  const escaped = pattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replaceAll('%', '.*').replaceAll('_', '.')
  return new RegExp(escaped, 'i').test(value)
}

export function filterProducts(data, params) {
  const q = params.get('q') || ''
  const server = params.get('server'), status = params.get('status'), change = params.get('change')
  const minimum = params.get('min_price'), maximum = params.get('max_price')
  const rows = data.items.filter(row =>
    (!q || like(q, row.title) || like(q, row.id)) &&
    (!server || row.server === server) && (!status || row.status === status) &&
    (!change || row.changes.includes(change)) &&
    (minimum == null || row.price >= Number(minimum)) && (maximum == null || row.price <= Number(maximum)))
  const sort = params.get('sort') || 'latest'
  const orders = {
    price_asc: ['price', 1], price_desc: ['price', -1], drop: ['delta', 1],
    drop_percent: ['percent', 1], delta_asc: ['delta', 1], delta_desc: ['delta', -1],
    total_delta_asc: ['total_delta', 1], total_delta_desc: ['total_delta', -1],
  }
  rows.sort((a, b) => {
    if (sort === 'latest') return compareText(b.first_seen, a.first_seen) || compareText(b.id, a.id)
    const [key, direction] = orders[sort] || orders.price_asc
    if (key === 'total_delta') {
      if (a[key] == null && b[key] != null) return 1
      if (b[key] == null && a[key] != null) return -1
    }
    return ((a[key] ?? 0) - (b[key] ?? 0)) * direction || compareText(a.id, b.id)
  })
  const page = Math.max(1, Number(params.get('page')) || 1)
  const size = Math.min(100, Math.max(1, Number(params.get('page_size')) || 20))
  return {...data, items: rows.slice((page - 1) * size, page * size), total: rows.length}
}

export function createStaticApi(base, fetcher = fetch) {
  let manifest, files = new Map()
  async function json(path, fresh = false) {
    const response = await fetcher(base + 'data/' + path, fresh ? {cache: 'no-store'} : {})
    if (!response.ok) throw new Error('看板数据暂不可用，请稍后刷新')
    return response.json()
  }
  async function refreshManifest() {
    const next = await json('manifest.json', true)
    if (next.format !== 1 || !/^[a-f0-9]{32}$/.test(next.version)) throw new Error('不支持的看板数据版本')
    if (manifest?.version !== next.version) files = new Map()
    manifest = next
  }
  async function versionFile(name, snapshot, cache) {
    if (!cache.has(name)) {
      const pending = json('versions/' + snapshot.version + '/' + name)
      cache.set(name, pending)
      pending.catch(() => cache.delete(name))
    }
    return cache.get(name)
  }
  return async function api(path, options = {}) {
    if (options.method && options.method !== 'GET') throw new Error('公开看板仅支持查看数据')
    const [route, query = ''] = path.split('?')
    const params = new URLSearchParams(query)
    if (!manifest || route === '/overview') await refreshManifest()
    const snapshot = manifest, cache = files
    const load = name => versionFile(name, snapshot, cache)
    if (route === '/overview') {
      const [overview, runs] = await Promise.all([load('overview.json'), load('runs.json')])
      return {...overview, publication: snapshot, last_attempt: runs.items[0] || null}
    }
    if (route === '/products') return filterProducts(await load('products.json'), params)
    if (route === '/settings') return {}
    if (route === '/crawls') {
      const runs = await load('runs.json'), page = Math.max(1, Number(params.get('page')) || 1)
      return {items: runs.items.slice((page - 1) * 20, page * 20), total: runs.items.length}
    }
    if (route === '/changes') {
      const events = await load('changes.json'), page = Math.max(1, Number(params.get('page')) || 1)
      if (params.has('run_id') && Number(params.get('run_id')) !== (await load('products.json')).run_id) {
        throw new Error('公开看板仅提供最近成功批次的变化列表')
      }
      return events.slice((page - 1) * 100, page * 100)
    }
    const product = route.match(/^\/products\/([^/]+)(\/history)?$/)
    if (product) {
      const id = decodeURIComponent(product[1])
      if (product[2]) {
        const file = snapshot.histories[id]
        if (!/^history\/[a-f0-9]{64}\.json$/.test(file || '')) throw new Error('商品不存在')
        return load(file)
      }
      const value = (await load('products.json')).items.find(row => row.id === id)
      if (!value) throw new Error('商品不存在')
      return value
    }
    const crawl = route.match(/^\/crawls\/(\d+)$/)
    if (crawl) {
      const run = (await load('runs.json')).items.find(row => row.id === Number(crawl[1]))
      if (!run) throw new Error('批次不存在')
      return {run, pages: []}
    }
    throw new Error('公开看板不支持此操作')
  }
}
