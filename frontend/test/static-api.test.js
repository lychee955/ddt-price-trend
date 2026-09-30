import assert from 'node:assert/strict'
import test from 'node:test'
import {createStaticApi, filterProducts} from '../src/static-api.js'

const data = {run_id: 2, servers: ['一区', '二区'], items: [
  {id:'1', title:'账号 Alpha', server:'一区', status:'listed', price:8000, delta:-2000, percent:-20, total_delta:-2000, first_seen:'2026-09-01', changes:['decreased']},
  {id:'2', title:'账号 Beta', server:'二区', status:'missing', price:20000, delta:null, percent:null, total_delta:1000, first_seen:'2026-09-02', changes:['suspected_missing']},
  {id:'3', title:'账号 Gamma', server:'一区', status:'publicity', price:15000, delta:3000, percent:25, total_delta:null, first_seen:'2026-09-02', changes:['increased']},
]}
const query = value => new URLSearchParams(value)

test('filters combine title, server, status, price in cents and change kind', () => {
  const rows = filterProducts(data, query('q=alpha&server=一区&status=listed&min_price=7000&max_price=9000&change=decreased'))
  assert.deepEqual(rows.items.map(row => row.id), ['1'])
  assert.equal(rows.total, 1)
  assert.deepEqual(filterProducts(data, query('q=2')).items.map(row => row.id), ['2'])
  assert.equal(filterProducts(data, query('q=unknown')).total, 0)
  assert.equal(filterProducts(data, query('q=%')).total, 3)
  assert.deepEqual(rows.servers, data.servers)
})

test('sorting matches null and tie behavior before pagination', () => {
  assert.deepEqual(filterProducts(data, query('sort=delta_asc')).items.map(row => row.id), ['1','2','3'])
  assert.deepEqual(filterProducts(data, query('sort=total_delta_desc')).items.map(row => row.id), ['2','1','3'])
  assert.deepEqual(filterProducts(data, query('sort=latest')).items.map(row => row.id), ['3','2','1'])
  const page = filterProducts(data, query('sort=price_asc&page=2&page_size=1'))
  assert.deepEqual(page.items.map(row => row.id), ['3'])
  assert.equal(page.total, 3)
  assert.deepEqual(data.items.map(row => row.id), ['1','2','3'])
})

test('versioned requests use the Pages subpath and change together', async () => {
  const calls = [], first = 'a'.repeat(32), second = 'b'.repeat(32), file = 'history/'+'c'.repeat(64)+'.json'
  let version = first
  const api = createStaticApi('/ddt-price-trend/', async (url, options) => {
    calls.push({url, options})
    const name = url.split('/').at(-1)
    const value = name === 'manifest.json' ? {format:1, version, histories:{'1':file}} :
      name === 'overview.json' ? {latest:{id:version === first ? 1 : 2}, runtime:{}} :
      name === 'products.json' ? data : name === 'runs.json' ? {items:[{id:2,status:'success'}]} :
      name === 'changes.json' ? [{id:3}] : {snapshots:[],events:[],gaps:[]}
    return {ok:true, json:async () => value}
  })
  assert.equal((await api('/overview')).latest.id, 1)
  await api('/products?sort=delta_desc')
  await api('/products/1/history')
  assert.ok(calls.some(call => call.url === '/ddt-price-trend/data/versions/'+first+'/'+file))
  const productCalls = calls.filter(call => call.url.endsWith('products.json')).length
  await api('/products')
  assert.equal(calls.filter(call => call.url.endsWith('products.json')).length, productCalls)
  version = second
  assert.equal((await api('/overview')).latest.id, 2)
  await api('/products')
  assert.ok(calls.some(call => call.url === '/ddt-price-trend/data/versions/'+second+'/products.json'))
  await assert.rejects(api('/crawls',{method:'POST'}), /仅支持查看/)
  await assert.rejects(api('/products/absent/history'), /不存在/)
})

test('failed downloads are not cached forever', async () => {
  let failed = true
  const api = createStaticApi('/', async url => ({
    ok: url.endsWith('manifest.json') || !failed,
    json: async () => url.endsWith('manifest.json') ? {format:1,version:'a'.repeat(32),histories:{}} : data,
  }))
  await assert.rejects(api('/products'), /暂不可用/)
  failed = false
  assert.equal((await api('/products')).total, 3)
})

test('overlapping refreshes keep each overview and publication on one version', async () => {
  const first = 'a'.repeat(32), second = 'b'.repeat(32)
  let version = first, releaseOld
  const oldData = new Promise(resolve => {releaseOld = resolve})
  const api = createStaticApi('/', async url => {
    if (url.endsWith('manifest.json')) return {ok:true,json:async () => ({format:1,version,histories:{}})}
    const old = url.includes(first)
    if (old) await oldData
    return {ok:true,json:async () => url.endsWith('runs.json') ? {items:[]} : {latest:{id:old?1:2}}}
  })
  const earlier = api('/overview')
  await new Promise(resolve => setImmediate(resolve))
  version = second
  const current = await api('/overview')
  releaseOld()
  const old = await earlier
  assert.equal(current.publication.version, second)
  assert.equal(current.latest.id, 2)
  assert.equal(old.publication.version, first)
  assert.equal(old.latest.id, 1)
})
