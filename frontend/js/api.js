export function createApi(getToken, onUnauthorized) {
  async function request(path, options = {}) {
    const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
    const token = getToken();
    if (token) headers.Authorization = "Bearer " + token;
    const res = await fetch(path, Object.assign({}, options, { headers }));
    if (res.status === 401) {
      onUnauthorized();
      throw new Error("未登录或令牌失效");
    }
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
    if (!res.ok) {
      const detail = data && (data.detail || data.message);
      let msg;
      if (typeof detail === "string") msg = detail;
      else if (detail && typeof detail === "object" && detail.message) msg = detail.message;
      else if (detail != null) { try { msg = JSON.stringify(detail); } catch { msg = "请求失败 " + res.status; } }
      else msg = "请求失败 " + res.status;
      throw new Error(msg);
    }
    return data;
  }
  return {
    login: (username, password) => request("/api/login", { method: "POST", body: JSON.stringify({ username, password }) }),
    me: () => request("/api/me"),
    dashboard: () => request("/api/dashboard"),
    plants: () => request("/api/plants"),
    plant: (id) => request("/api/plants/" + id),
    createPlant: (body) => request("/api/plants", { method: "POST", body: JSON.stringify(body) }),
    outfalls: () => request("/api/outfalls"),
    outfall: (id) => request("/api/outfalls/" + id),
    createOutfall: (body) => request("/api/outfalls", { method: "POST", body: JSON.stringify(body) }),
    setOutfallStatus: (id, status) => request("/api/outfalls/" + id + "/status?status=" + encodeURIComponent(status), { method: "POST" }),
    samples: (status) => request("/api/samples" + (status ? ("?status=" + encodeURIComponent(status)) : "")),
    sample: (id) => request("/api/samples/" + id),
    createSample: (body) => request("/api/samples", { method: "POST", body: JSON.stringify(body) }),
    exceeds: (status) => request("/api/exceeds" + (status ? ("?status=" + encodeURIComponent(status)) : "")),
    closeExceed: (body) => request("/api/exceeds/close", { method: "POST", body: JSON.stringify(body) }),
    openExceed: (body) => request("/api/exceeds/open", { method: "POST", body: JSON.stringify(body) }),
    releases: () => request("/api/releases"),
    createRelease: (body) => request("/api/releases", { method: "POST", body: JSON.stringify(body) }),
    seals: () => request("/api/seals"),
    createSeal: (body) => request("/api/seals", { method: "POST", body: JSON.stringify(body) }),
    permits: () => request("/api/permits"),
    permit: (code) => request("/api/permits/" + encodeURIComponent(code)),
    createPermit: (body) => request("/api/permits", { method: "POST", body: JSON.stringify(body) }),
    lineage: () => request("/api/lineage"),
    createLineage: (body) => request("/api/lineage", { method: "POST", body: JSON.stringify(body) }),
    forceDeleteLineage: (id, mode) => request("/api/lineage/" + id + "/force-delete" + (mode ? ("?mode=" + encodeURIComponent(mode)) : ""), { method: "POST", body: JSON.stringify({ mode: mode || null }) }),
    lineageShadows: () => request("/api/lineage/shadows"),
    promoteShadow: (id) => request("/api/lineage/shadows/" + id + "/promote", { method: "POST", body: "{}" }),
    reconcileOrphans: () => request("/api/reconcile/orphans"),
    reconcile: () => request("/api/reconcile"),
    reconcileOne: (code) => request("/api/reconcile/" + encodeURIComponent(code)),
    proof: (code) => request("/api/proof/sample/" + encodeURIComponent(code)),
    labJobs: () => request("/api/stubs/lab-courier"),
    labEnqueue: (body) => request("/api/stubs/lab-courier", { method: "POST", body: JSON.stringify(body) }),
    sludgeJobs: () => request("/api/stubs/sludge-plan"),
    sludgeEnqueue: (body) => request("/api/stubs/sludge-plan", { method: "POST", body: JSON.stringify(body) }),
    audit: () => request("/api/audit"),
  };
}
