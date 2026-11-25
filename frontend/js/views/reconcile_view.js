export const reconcileViewTemplate = `
  <div class="grid-2">
    <div class="panel">
      <h3>许可对账</h3>
      <div class="actions"><button class="secondary" @click="loadReconcile">刷新全部</button></div>
      <table>
        <thead><tr><th>许可</th><th>对齐</th><th>证明</th><th>封批数</th><th>孤儿指标</th></tr></thead>
        <tbody>
          <tr v-for="r in reconAllItems" :key="r.permit_code">
            <td>{{ r.permit_code }}</td>
            <td :class="r.balanced?'pass':'fail'">{{ r.balanced ? "齐" : "不齐" }}</td>
            <td :class="r.proof_ready?'pass':'warn'">{{ r.proof_ready ? "可证" : "未齐" }}</td>
            <td>{{ r.seal_count || 0 }}</td>
            <td :class="r.orphan_metrics_ok===false ? 'fail' : 'pass'">
              {{ r.orphan_metrics_ok === false ? ("未清 " + (r.orphan_count||0)) : "无孤儿" }}
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="reconOrphans.length" class="banner err" style="margin-top:0.8rem">
        <strong>孤儿指标（影子断链未升正，对账不计价）：</strong>
        <span v-for="o in reconOrphans" :key="(o.shadow_id||'')+':'+o.sample_code" style="display:block">
          · {{ o.sample_code }}（{{ o.permit_ref }}）— {{ o.reason }}
        </span>
      </div>
    </div>
    <div class="panel">
      <h3>单许可 / 采样证明</h3>
      <label>许可号</label><input v-model="reconCode"/>
      <div class="actions"><button @click="loadReconcileOne">对账</button></div>
      <div v-if="reconOne" style="margin:0.6rem 0">
        <span :class="reconOne.orphan_metrics_ok ? 'pass' : 'fail'" style="font-weight:600">
          {{ reconOne.orphan_metrics_ok ? "孤儿指标清" : ("孤儿指标未清 ×" + reconOne.orphan_count) }}
        </span>
        <span class="muted" style="margin-left:0.6rem">
          对齐 {{ reconOne.balanced ? "齐" : "不齐" }} · 证明 {{ reconOne.proof_ready ? "可证" : "未齐" }}
        </span>
        <div v-if="reconOne.orphan_metrics && reconOne.orphan_metrics.length" style="margin-top:0.4rem">
          <div v-for="o in reconOne.orphan_metrics" :key="o.sample_code" class="fail">
            不计价：{{ o.sample_code }} — {{ o.reason }}
          </div>
        </div>
      </div>
      <pre v-if="reconOne" style="white-space:pre-wrap;font-size:0.82rem">{{ JSON.stringify(reconOne, null, 2) }}</pre>
      <label>采样号证明</label><input v-model="proofCode"/>
      <div class="actions"><button class="secondary" @click="loadProof">证明</button></div>
      <pre v-if="proofOne" style="white-space:pre-wrap;font-size:0.82rem">{{ JSON.stringify(proofOne, null, 2) }}</pre>
    </div>
  </div>`;
