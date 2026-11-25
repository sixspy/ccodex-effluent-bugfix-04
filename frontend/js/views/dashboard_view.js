export const dashboardViewTemplate = `
  <section class="hero-strip">
    <h2>污水排放批总览</h2>
    <p>采样入库 → 超标挂起 → 准排裁定 → 封批升代 → 许可对账</p>
  </section>
  <div class="stats">
    <div class="stat"><div class="n">{{ dash.plants || 0 }}</div><div class="l">处理厂</div></div>
    <div class="stat"><div class="n">{{ dash.outfalls || 0 }}</div><div class="l">排放口</div></div>
    <div class="stat"><div class="n">{{ dash.samples || 0 }}</div><div class="l">采样批次</div></div>
    <div class="stat"><div class="n">{{ dash.open_exceeds || 0 }}</div><div class="l">开放超标</div></div>
    <div class="stat"><div class="n">{{ dash.discharge_releases || 0 }}</div><div class="l">已准排</div></div>
    <div class="stat"><div class="n">{{ dash.discharge_seals || 0 }}</div><div class="l">已封批</div></div>
    <div class="stat"><div class="n">{{ dash.permits || 0 }}</div><div class="l">许可册</div></div>
    <div class="stat"><div class="n">{{ dash.side_pending || 0 }}</div><div class="l">旁路挂起</div></div>
    <div class="stat"><div class="n" :class="(dash.voided_samples||0) ? 'muted' : ''">{{ dash.voided_samples || 0 }}</div><div class="l">级联作废指标</div></div>
    <div class="stat"><div class="n" :class="(dash.open_shadow_breaks||0) ? 'fail' : 'pass'">{{ dash.open_shadow_breaks || 0 }}</div><div class="l">未清影子断链</div></div>
  </div>
  <div v-if="(dash.open_shadow_breaks||0)>0" class="banner err">
    总览预警：{{ dash.open_shadow_breaks }} 条影子断链未升正，对账不计孤儿指标，详见血缘/对账页。
  </div>
    <div class="panel">
      <h3>产品骨架</h3>
      <p class="muted">{{ (dash.meta && dash.meta.skeleton) || "—" }}</p>
    </div>`;
