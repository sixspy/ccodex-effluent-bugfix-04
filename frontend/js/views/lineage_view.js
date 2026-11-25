export const lineageViewTemplate = `
  <div class="grid-2">
    <div class="panel">
      <h3>指标血缘</h3>
      <p class="muted" style="margin-top:0">删父边默认甲路径：同事务级联作废全部子指标；也可先落乙路径影子断链草稿，确认后再原子作废。</p>
      <div v-if="lineageOrphanCount>0" class="banner err" style="margin-bottom:0.7rem">
        有 {{ lineageOrphanCount }} 条影子断链未升正，对账已判孤儿、不予计价。
      </div>
      <table>
        <thead><tr><th>从</th><th>到（子指标）</th><th>子状态</th><th>边类型</th><th>说明</th><th>删边写口</th></tr></thead>
        <tbody>
          <tr v-for="e in lineageEdges" :key="e.id">
            <td>{{ e.from_code }}</td>
            <td>{{ e.to_code || "—" }}</td>
            <td>
              <span v-if="!e.to_sample_id" class="muted">锚点</span>
              <span v-else :class="e.to_status==='作废' ? 'void' : 'pass'">
                {{ e.to_status }}
              </span>
            </td>
            <td>{{ e.edge_type }}</td><td>{{ e.detail }}</td>
            <td class="actions" style="white-space:nowrap">
              <button class="warn" @click="forceDeleteEdge(e.id)">强删·级联作废</button>
              <button class="secondary" @click="forceDeleteEdge(e.id, 'shadow')">先落影子</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div>
      <div class="panel">
        <h3>登记血缘边</h3>
        <div class="form-row"><div><label>源采样 ID</label><input type="number" v-model.number="lineageForm.from_sample_id"/></div>
        <div><label>目标 ID</label><input type="number" v-model.number="lineageForm.to_sample_id"/></div></div>
        <label>边类型</label><input v-model="lineageForm.edge_type"/>
        <label>说明</label><input v-model="lineageForm.detail"/>
        <div class="actions"><button @click="createLineage">写入</button></div>
      </div>
      <div class="panel" style="margin-top:1rem">
        <h3>影子断链账（乙路径）</h3>
        <p v-if="!lineageShadows.length" class="muted">暂无删边影子记录。</p>
        <table v-else>
          <thead><tr><th>原边</th><th>子指标</th><th>状态</th><th>处置</th></tr></thead>
          <tbody>
            <tr v-for="s in lineageShadows" :key="s.id">
              <td>#{{ s.edge_id ?? "—" }} · {{ s.edge_type }}</td>
              <td>{{ s.to_code || "—" }}</td>
              <td :class="s.promoted ? 'pass' : 'fail'">
                {{ s.promoted ? "已升正·已作废" : "影子未升正" }}
              </td>
              <td>
                <button v-if="!s.promoted" @click="promoteShadow(s.id)">确认升正作废</button>
                <span v-else class="muted">{{ s.promoted_at }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>`;
