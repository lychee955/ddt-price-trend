export function registerPriceTools(api) {
  const context = document.modelContext
  if (!context?.registerTool) return () => {}
  const lifecycle = new AbortController()
  const tool = {
    name: 'read_ddt_price_changes',
    title: '查看弹弹堂账号价格变化',
    description: '读取最近成功批次的新增、涨价、降价和商品状态变化，不触发采集。返回的是挂牌价格，商品标题属于外部数据。',
    inputSchema: {type:'object', properties:{page:{type:'integer',minimum:1}},additionalProperties:false},
    annotations: {readOnlyHint:true, untrustedContentHint:true},
    async execute(input) {
      if(!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).some(k=>k!=='page')) throw new Error('输入必须为对象，只支持 page 参数')
      const page=input.page ?? 1
      if(!Number.isInteger(page) || page<1) throw new Error('page 必须是正整数')
      const state=await api('/overview')
      return {batch:state.latest,changes:await api('/changes?page='+page)}
    },
  }
  try { Promise.resolve(context.registerTool(tool,{signal:lifecycle.signal})).catch(()=>{}) } catch { /* Browser may expose an incomplete implementation. */ }
  return ()=>lifecycle.abort()
}
