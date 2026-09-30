<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { registerPriceTools } from './webmcp'
import { createStaticApi } from './static-api'
echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])

const readonly = import.meta.env.VITE_STATIC_MODE === 'true'
const staticApi = readonly ? createStaticApi(import.meta.env.BASE_URL) : null
const clock = ref(Date.now())

const view = ref('products'), overview = ref(null), products = ref([]), total = ref(0), servers = ref([])
const loading = ref(false), submitting = ref(false), saving = ref(false), error = ref(''), authorized = ref(true)
const token = ref(sessionStorage.getItem('ddt-token') || '')
const filters = reactive({q:'', server:'', status:'', change:'', sort:'latest', page:1, min_price:null, max_price:null})
const productTable = ref(null)
const tableSorts = {
  drop: {prop:'delta', order:'ascending'},
  delta_asc: {prop:'delta', order:'ascending'},
  delta_desc: {prop:'delta', order:'descending'},
  total_delta_asc: {prop:'total_delta', order:'ascending'},
  total_delta_desc: {prop:'total_delta', order:'descending'},
}
function sortProducts({prop, order}) {
  const current = tableSorts[filters.sort]
  if (current?.prop === prop && current?.order === order) return
  filters.sort = order && ['delta','total_delta'].includes(prop) ? prop+(order==='ascending'?'_asc':'_desc') : 'latest'
  filters.page = 1
  search()
}
watch([productTable, () => filters.sort], () => {
  const current = tableSorts[filters.sort]
  if (current) productTable.value?.sort(current.prop, current.order)
  else productTable.value?.clearSort()
}, {flush:'post'})
const runs = ref([]), runTotal = ref(0), runPage = ref(1), config = reactive({})
const detail = ref(null), history = ref(null), detailOpen = ref(false), detailLoading = ref(false)
const runDetail = ref(null), runOpen = ref(false), chartElement = ref(null)
let chart, timer, fetching = false, sequence = 0, unregisterTools
const labels = {listed:'上架中',publicity:'公示期',new:'新发现',decreased:'降价',increased:'涨价',returned:'重新出现',status_changed:'状态变更',suspected_missing:'疑似下架',missing:'持续未发现',delisted:'确认下架',sold:'已售出',unchanged:'未变化',baseline:'初始基线',queued:'等待采集',running:'采集中',success:'成功',failed:'失败',blocked:'访问受限',interrupted:'已中断',manual:'手动',scheduled:'定时',cli:'命令行'}
const label = x => labels[x] || x
const money = x => x == null ? '—' : new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format(x/100)
const date = x => x ? new Date(x).toLocaleString('zh-CN',{hour12:false}) : '—'
const color = x => ['decreased','success'].includes(x) ? 'success' : ['increased','failed','blocked'].includes(x) ? 'danger' : ['missing','suspected_missing','interrupted'].includes(x) ? 'warning' : 'info'
const latest = computed(() => overview.value?.latest)
const summary = computed(() => latest.value?.summary || {})
const cooldown = computed(() => overview.value?.runtime?.cooldown_until && new Date(overview.value.runtime.cooldown_until) > new Date())
const online = computed(() => overview.value?.runtime?.worker_online)
const active = computed(() => overview.value?.active)
const stale = computed(() => readonly && (!latest.value?.finished_at || clock.value - Date.parse(latest.value.finished_at) > 5 * 60 * 60 * 1000))
const visibleCount = computed(() => (overview.value?.counts?.listed || 0) + (overview.value?.counts?.publicity || 0))
const missingCount = computed(() => (summary.value.suspected_missing || 0) + (summary.value.missing || 0) + (summary.value.delisted || 0))

async function api(path, options={}) {
  if (readonly) return staticApi(path, options)
  const response = await fetch('/api'+path, {...options, headers:{'Content-Type':'application/json','X-DDT-Client':'dashboard', ...(token.value ? {Authorization:'Bearer '+token.value} : {}), ...options.headers}})
  if (response.status === 401) { authorized.value=false; throw new Error('请先输入访问令牌') }
  const data = await response.json()
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : data.detail?.map(x=>x.msg).join('；') || '请求失败')
  return data
}
async function loadProducts() {
  const request = ++sequence
  const params = new URLSearchParams()
  Object.entries(filters).forEach(([k,v]) => { if(v!=='' && v!=null) params.set(k, k.endsWith('_price') ? Math.round(v*100) : v) })
  const data = await api('/products?'+params)
  if (request !== sequence) return
  products.value=data.items; total.value=data.total; servers.value=data.servers
}
async function loadRuns() {
  const data = await api('/crawls?page='+runPage.value); runs.value=data.items; runTotal.value=data.total
}
async function refresh(initial=false) {
  if(fetching) return
  fetching=true
  if(initial) loading.value=true
  try {
    const prevId=latest.value?.id
    overview.value=await api('/overview')
    if(initial || view.value==='products' && latest.value?.id!==prevId) await loadProducts()
    if(view.value==='runs' || initial) await loadRuns()
    if(initial) Object.assign(config, await api('/settings'))
    error.value=''; authorized.value=true
  } catch(e) {error.value=e.message}
  finally {fetching=false; loading.value=false}
}
async function search() {loading.value=true; try {await loadProducts()} catch(e) {error.value=e.message} finally {loading.value=false}}
async function trigger() {
  submitting.value=true
  try {await api('/crawls',{method:'POST'}); ElMessage.success('采集任务已提交'); await refresh(); await loadRuns()}
  catch(e) {ElMessage.error(e.message)} finally {submitting.value=false}
}
async function save() {
  saving.value=true
  try {Object.assign(config,await api('/settings',{method:'PATCH',body:JSON.stringify(config)})); ElMessage.success('采集设置已保存'); await refresh()}
  catch(e) {ElMessage.error(e.message)} finally {saving.value=false}
}
async function login() {sessionStorage.setItem('ddt-token',token.value); await refresh(true)}
async function showProduct(row) {
  detail.value=row; detailOpen.value=true; detailLoading.value=true; history.value=null
  try {history.value=await api('/products/'+row.id+'/history'); await nextTick(); drawChart()}
  catch(e) {ElMessage.error(e.message)} finally {detailLoading.value=false}
}
function drawChart() {
  if(!chartElement.value || !history.value) return
  chart?.dispose(); chart=echarts.init(chartElement.value)
  const points=[...history.value.snapshots.map(x=>({time:x.observed_at,price:x.price})),...history.value.gaps.map(x=>({time:x.started_at || x.finished_at,price:null}))].sort((a,b)=>a.time.localeCompare(b.time))
  chart.setOption({color:['#4169e1'],tooltip:{trigger:'axis',valueFormatter:v=>v==null?'未采集到价格':'¥'+v},grid:{left:65,right:24,top:24,bottom:70},xAxis:{type:'category',data:points.map(x=>date(x.time)),axisLabel:{rotate:20,fontSize:12}},yAxis:{type:'value',scale:true,axisLabel:{formatter:'¥{value}'},splitLine:{lineStyle:{color:'#e8edf4'}}},series:[{type:'line',data:points.map(x=>x.price==null?null:x.price/100),connectNulls:false,symbolSize:7,step:'end',areaStyle:{opacity:0.06}}]})
}
async function showRun(row) {try {runDetail.value=await api('/crawls/'+row.id); runOpen.value=true} catch(e) {ElMessage.error(e.message)}}
function chooseChange(change) {filters.change=filters.change===change?'':change; filters.page=1; search()}
function resize(){chart?.resize()}
watch(view, async v => {try {if(v==='runs') await loadRuns(); if(v==='products') await loadProducts()} catch(e){error.value=e.message}})
onMounted(()=>{refresh(true);timer=setInterval(()=>{clock.value=Date.now();refresh()},readonly?60000:5000);window.addEventListener('resize',resize);unregisterTools=registerPriceTools(api)})
onBeforeUnmount(()=>{clearInterval(timer);chart?.dispose();window.removeEventListener('resize',resize);unregisterTools?.()})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">弹</span><div>号价观察<small>弹弹堂 · 4399 游戏店</small></div></div>
      <div class="nav-caption">工作台</div>
      <button :class="['nav-item',{selected:view==='products'}]" @click="view='products'"><span>▦</span> 商品看板</button>
      <button :class="['nav-item',{selected:view==='runs'}]" @click="view='runs'"><span>◷</span> {{readonly?'采集记录':'采集管理'}}</button>
      <div class="sidebar-bottom"><span class="source-badge">80</span><div>只关注弹弹堂账号<small>上架中 · 公示期</small></div></div>
    </aside>
    <main>
      <header class="page-header"><div><div class="eyebrow">ACCOUNT PRICE MONITOR</div><h1>{{view==='products'?'商品看板':readonly?'采集记录':'采集管理'}}</h1><p>{{view==='products'?'关注每一次价格变化。':readonly?'查看每一批次的执行结果。':'管理采集计划，查看每一批次的执行结果。'}}</p></div><div class="header-actions"><template v-if="readonly"><el-tag :type="stale?'warning':'success'" effect="plain">{{stale?'数据待更新':'只读看板'}}</el-tag><el-button @click="refresh(true)">刷新数据</el-button></template><template v-else><el-tag :type="online?'success':'info'" effect="plain">{{online?'采集服务在线':'采集服务离线'}}</el-tag><el-button type="primary" size="large" :loading="submitting" :disabled="!!active || !!cooldown || !online" @click="trigger">{{active?'正在采集…':'立即采集'}}</el-button></template></div></header>

      <el-alert v-if="error && authorized" :title="error" type="error" show-icon :closable="false" class="notice"/>
      <el-alert v-if="!readonly && !online && authorized && overview" title="采集服务未运行。请使用 uv run ddt serve 启动完整程序，或单独运行 uv run ddt worker。" type="warning" :closable="false" class="notice"/>
      <el-alert v-if="readonly && overview" :title="'最后成功采集：'+date(latest?.finished_at)+'。计划每 2 小时更新，实际时间可能延迟。'" :type="stale?'warning':'info'" :closable="false" class="notice"/>
      <el-alert v-if="readonly && overview?.last_attempt && overview.last_attempt.status!=='success'" :title="'最近一次采集：'+label(overview.last_attempt.status)+'，继续展示上次成功结果。'" type="warning" :closable="false" class="notice"/>
      <el-alert v-if="cooldown" :title="'采集已暂停，冷却至 '+date(overview.runtime.cooldown_until)+'。'+(overview.runtime.last_error || '')" type="warning" :closable="false" class="notice"/>
      <div v-if="active" class="progress-strip"><span class="pulse"></span><strong>批次 #{{active.id}} · {{label(active.status)}}</strong><span>已完成 {{active.pages_done}} 页 · 发现 {{active.product_count}} 个商品</span><span>采集完成后自动更新</span></div>

      <section v-if="view==='products'">
        <div class="stats">
          <button class="stat-card" @click="filters.change='';filters.status='';filters.page=1;search()"><span>当前列表商品</span><strong>{{visibleCount}}<small>个</small></strong><em>上架中 + 公示期</em></button>
          <button :class="['stat-card',{chosen:filters.change==='new'}]" @click="chooseChange('new')"><span>本次新发现</span><strong>{{summary.new || 0}}<small>个</small></strong><em>系统首次观察到</em></button>
          <button :class="['stat-card green',{chosen:filters.change==='decreased'}]" @click="chooseChange('decreased')"><span>本次降价</span><strong>{{summary.decreased || 0}}<small>个</small></strong><em>点击查看降价账号</em></button>
          <button :class="['stat-card red',{chosen:filters.change==='increased'}]" @click="chooseChange('increased')"><span>本次涨价</span><strong>{{summary.increased || 0}}<small>个</small></strong><em>相较上次观察价格</em></button>
          <button class="stat-card" @click="chooseChange('suspected_missing')"><span>本次下架相关变化</span><strong>{{missingCount}}<small>个</small></strong><em>疑似、持续未发现及确认下架</em></button>
        </div>
        <div class="panel">
          <div class="panel-heading"><h2>账号列表 <span>{{total}}</span></h2><div class="batch-note" v-if="latest">批次 #{{latest.id}} {{latest.base_run_id?'对比 #'+latest.base_run_id:'· 初始基线'}}<span>{{date(latest.finished_at)}}</span></div><span v-else class="muted">尚未建立价格基线</span></div>
          <form class="filters" @submit.prevent="filters.page=1;search()">
            <el-input v-model="filters.q" placeholder="搜索账号标题 / 商品 ID" clearable aria-label="搜索账号" class="search-field"/>
            <el-select v-model="filters.server" placeholder="全部区服" clearable aria-label="筛选区服"><el-option v-for="s in servers" :key="s" :label="s" :value="s"/></el-select>
            <el-select v-model="filters.change" placeholder="全部变化" clearable aria-label="筛选变化"><el-option v-for="s in ['new','decreased','increased','returned','suspected_missing','missing','delisted','sold','status_changed']" :key="s" :label="label(s)" :value="s"/></el-select>
            <el-select v-model="filters.status" placeholder="全部状态" clearable aria-label="筛选状态"><el-option v-for="s in ['listed','publicity','suspected_missing','missing','delisted','sold']" :key="s" :label="label(s)" :value="s"/></el-select>
            <el-button native-type="submit" type="primary" plain>筛选</el-button>
            <div class="filter-second"><span>价格范围</span><el-input-number v-model="filters.min_price" :min="0" :controls="false" placeholder="最低 ¥" aria-label="最低价格"/><span>至</span><el-input-number v-model="filters.max_price" :min="0" :controls="false" placeholder="最高 ¥" aria-label="最高价格"/><el-select v-model="filters.sort" aria-label="排序方式" @change="filters.page=1;search()"><el-option label="最近发现" value="latest"/><el-option label="价格从低到高" value="price_asc"/><el-option label="价格从高到低" value="price_desc"/><el-option label="本次涨跌金额升序" value="delta_asc"/><el-option label="本次涨跌金额降序" value="delta_desc"/><el-option label="总涨跌金额升序" value="total_delta_asc"/><el-option label="总涨跌金额降序" value="total_delta_desc"/><el-option label="降价金额优先" value="drop"/><el-option label="降价比例优先" value="drop_percent"/></el-select><el-button text @click="Object.assign(filters,{q:'',server:'',change:'',status:'',min_price:null,max_price:null,sort:'latest',page:1});search()">重置</el-button></div>
          </form>
          <el-table ref="productTable" :data="products" v-loading="loading" class="product-table" row-key="id" @row-click="showProduct" @sort-change="sortProducts">
            <el-table-column label="账号 / 区服" min-width="310"><template #default="{row}"><button class="product-title" @click.stop="showProduct(row)">{{row.title}}</button><div class="product-meta">{{row.server}} <span>#{{row.id}}</span></div></template></el-table-column>
            <el-table-column label="当前 / 最后价格" width="155"><template #default="{row}"><strong class="price">{{money(row.price)}}</strong><div class="product-meta">{{['listed','publicity'].includes(row.status)?'上次 '+money(row.previous_price):'最后一次观察价'}}</div></template></el-table-column>
            <el-table-column prop="delta" label="本次涨跌" sortable="custom" width="155"><template #header><el-tooltip content="点击按本次涨跌金额排序；无本次调价记录按 0 排序"><span>本次涨跌</span></el-tooltip></template><template #default="{row}"><span :class="row.delta<0?'down':row.delta>0?'up':'muted'">{{row.delta==null?'—':(row.delta>0?'+':'')+money(row.delta)}}<small v-if="row.percent!=null">{{row.percent>0?'+':''}}{{row.percent}}%</small></span></template></el-table-column>
            <el-table-column prop="total_delta" label="总涨跌" sortable="custom" width="155"><template #header><el-tooltip content="当前 / 最后价格相较首次收录价格的变化；点击按总涨跌金额排序"><span>总涨跌</span></el-tooltip></template><template #default="{row}"><span class="total-change" :class="row.total_delta<0?'down':row.total_delta>0?'up':'muted'">{{row.total_delta==null?'—':(row.total_delta>0?'+':'')+money(row.total_delta)}}<small v-if="row.total_percent!=null">{{row.total_percent>0?'+':''}}{{row.total_percent}}%</small></span></template></el-table-column>
            <el-table-column label="状态与变化" min-width="165"><template #default="{row}"><div class="tags"><el-tag :type="color(row.status)" size="small" effect="plain">{{label(row.status)}}</el-tag><el-tag v-for="c in row.changes.filter(x=>x!==row.status)" :key="c" :type="color(c)" size="small">{{label(c)}}</el-tag></div></template></el-table-column>
            <el-table-column label="最后发现" width="175"><template #default="{row}"><span class="last-seen">{{date(row.last_seen)}}</span></template></el-table-column>
            <el-table-column width="80"><template #default="{row}"><el-button link type="primary" @click.stop="showProduct(row)">历史</el-button></template></el-table-column>
            <template #empty><el-empty :description="latest?'没有符合筛选条件的账号':readonly?'尚无成功采集数据，请等待首次更新':'点击「立即采集」，建立第一份价格快照'" :image-size="90"/></template>
          </el-table>
          <div class="table-footer"><span>挂牌价格 · 不代表成交价格</span><el-pagination v-model:current-page="filters.page" :page-size="20" :total="total" layout="prev,pager,next" @current-change="search"/></div>
        </div>
      </section>

      <section v-else class="management" :class="{readonly}">
        <div v-if="!readonly" class="panel settings-panel"><div class="panel-heading"><h2>自动采集</h2><el-tag effect="plain">{{config.enabled?'已开启':'已关闭'}}</el-tag></div><el-form label-position="top" class="settings-form">
          <el-form-item label="定时采集"><el-switch v-model="config.enabled"/><span class="muted switch-note">电脑需要保持运行</span></el-form-item>
          <el-form-item label="采集间隔（分钟）"><el-input-number v-model="config.interval_minutes" :min="15" :max="10080"/></el-form-item>
          <el-form-item label="每个请求的随机间隔（秒）"><div class="range-input"><el-input-number v-model="config.delay_min" :min="2" :max="60"/><span>至</span><el-input-number v-model="config.delay_max" :min="config.delay_min || 2" :max="120"/></div></el-form-item>
          <el-form-item label="网络错误最大重试次数"><el-input-number v-model="config.retries" :min="0" :max="3"/></el-form-item>
          <el-form-item label="触发拦截后的冷却时间（分钟）"><el-input-number v-model="config.cooldown_minutes" :min="15" :max="1440"/></el-form-item>
          <el-form-item label="数量异常下降保护阈值"><el-input-number v-model="config.drop_threshold" :min="0.1" :max="1" :step="0.05" :precision="2"/><span class="helper">0.30 表示下降超过 30% 时不发布结果；1.00 关闭数量下降保护。</span></el-form-item>
          <el-button type="primary" :loading="saving" @click="save">保存设置</el-button><p class="helper">下次计划：{{date(overview?.runtime?.next_run_at)}}<br>限流时遵守网站要求，手动采集也受冷却限制。</p>
        </el-form></div>
        <div class="panel history-panel"><div class="panel-heading"><h2>采集批次</h2><span class="muted">共 {{runTotal}} 次</span></div><el-table :data="runs" @row-click="showRun"><el-table-column label="批次" width="90"><template #default="{row}">#{{row.id}}</template></el-table-column><el-table-column label="时间 / 触发方式" min-width="195"><template #default="{row}">{{date(row.created_at)}}<div class="product-meta">{{label(row.trigger)}}</div></template></el-table-column><el-table-column label="结果" width="115"><template #default="{row}"><el-tag :type="color(row.status)">{{label(row.status)}}</el-tag></template></el-table-column><el-table-column label="页 / 商品" width="100"><template #default="{row}">{{row.pages_done}} / {{row.product_count}}</template></el-table-column><el-table-column label="摘要" min-width="180"><template #default="{row}"><span v-if="row.error" class="error-text">{{row.error}}</span><span v-else>{{Object.entries(row.summary).map(([k,v])=>label(k)+' '+v).join(' · ') || '等待执行'}}</span></template></el-table-column><template #empty><el-empty description="还没有采集记录" :image-size="80"/></template></el-table><div class="table-footer"><span>点击批次查看请求日志</span><el-pagination v-model:current-page="runPage" :page-size="20" :total="runTotal" layout="prev,pager,next" @current-change="loadRuns"/></div></div>
      </section>
      <footer>数据来源：<a href="https://www.youxidian.com/goods/search.html?game_id=80&cat_id=1" target="_blank" rel="noopener noreferrer">4399 游戏店</a><span>消失不等于成交；采集失败不会更新比较基准。</span></footer>
    </main>
  </div>

  <el-drawer v-model="detailOpen" size="min(820px, 100vw)" title="账号价格历史" @opened="drawChart" @closed="chart?.dispose()"><div v-if="detail" v-loading="detailLoading"><div class="detail-heading"><el-tag effect="plain">{{label(detail.status)}}</el-tag><h2>{{detail.title}}</h2><p>{{detail.server}} · #{{detail.id}}</p><strong>{{money(detail.price)}}</strong><a :href="detail.url" target="_blank" rel="noopener noreferrer">查看原商品 ↗</a></div><div ref="chartElement" class="price-chart" role="img" aria-label="账号历史挂牌价格折线图"></div><p class="helper">价格按采集时刻记录，空缺表示未发现商品或采集失败。首次发现：{{date(detail.first_seen)}}</p><h3>观察记录</h3><el-table :data="[...(history?.snapshots || [])].reverse()" max-height="350"><el-table-column label="时间" min-width="170"><template #default="{row}">{{date(row.observed_at)}}</template></el-table-column><el-table-column label="价格" width="120"><template #default="{row}">{{money(row.price)}}</template></el-table-column><el-table-column label="状态 / 依据" min-width="180"><template #default="{row}">{{label(row.source_status)}}<div class="product-meta">{{row.evidence}}</div></template></el-table-column></el-table><h3>变化时间线</h3><el-timeline v-if="history?.events.length"><el-timeline-item v-for="e in history.events" :key="e.id" :timestamp="date(e.created_at)">{{label(e.kind)}}<span v-if="e.delta"> · {{money(e.old_price)}} → {{money(e.new_price)}}</span><span v-else-if="e.old_status"> · {{label(e.old_status)}} → {{label(e.new_status)}}</span></el-timeline-item></el-timeline><p v-else class="muted">尚无变化记录，首次采集作为基线。</p></div></el-drawer>
  <el-drawer v-model="runOpen" size="min(760px, 100vw)" title="采集批次详情"><template v-if="runDetail"><h2>批次 #{{runDetail.run.id}} · {{label(runDetail.run.status)}}</h2><p>开始：{{date(runDetail.run.started_at)}}<br>结束：{{date(runDetail.run.finished_at)}}</p><el-alert v-if="runDetail.run.error" :title="runDetail.run.error" type="error" :closable="false"/><div v-for="p in runDetail.pages" :key="p.id" class="log-row"><el-tag :type="p.status==='failed'?'danger':'info'">{{({parsed:'已解析',rechecked:'已复核',retry:'重试',failed:'失败',detail:'详情检查',detail_unknown:'详情未确认'})[p.status] || p.status}}</el-tag><small>{{date(p.created_at)}} · {{p.count ?? '—'}} 个</small><code>{{p.url}}</code><p v-if="p.error">{{p.error}}</p></div></template></el-drawer>
  <el-dialog :model-value="!authorized" title="访问号价观察" width="min(440px, 95vw)" :show-close="false" :close-on-click-modal="false" :close-on-press-escape="false"><p>输入部署时设置的访问令牌。</p><el-input v-model="token" type="password" show-password placeholder="访问令牌" aria-label="访问令牌" @keyup.enter="login"/><p v-if="error" class="error-text">{{error}}</p><template #footer><el-button type="primary" @click="login">进入管理页面</el-button></template></el-dialog>
</template>

<style scoped>
.management.readonly { grid-template-columns: 1fr; }
.total-change small { display: block; font-size: 12px; margin-top: 4px; font-weight: 400; }
</style>
