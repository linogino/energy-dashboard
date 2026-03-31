'use strict';

/**
 * chain-graph.js — グラフデータ読み込み・マージ・クエリ層
 *
 * UI・simulation から分離したデータ層。
 * ChainGraph.load() → グラフオブジェクトを返す。
 * グラフオブジェクトは immutable に扱うこと。
 */
const ChainGraph = (() => {

  const SOURCES_FILE  = 'data/chains/sources.json';
  const CHAIN_FILES   = [
    'data/chains/ethylene-core.json',
    'data/chains/ethylene-eo.json',
    'data/chains/ethylene-downstream.json',
  ];

  // ── Load ────────────────────────────────────────────────────────────────

  /**
   * 全チェーンファイルを並列取得してマージしたグラフを返す
   * @returns {Promise<Graph>}
   */
  async function load() {
    const responses = await Promise.all(
      [SOURCES_FILE, ...CHAIN_FILES].map(f => fetch(f))
    );
    for (const r of responses) {
      if (!r.ok) throw new Error(`ChainGraph: fetch failed — ${r.url}`);
    }
    const [sourcesData, ...modules] = await Promise.all(
      responses.map(r => r.json())
    );
    return _build(sourcesData, modules);
  }

  // ── Build ────────────────────────────────────────────────────────────────

  function _build(sourcesData, modules) {
    /** @type {Graph} */
    const g = {
      sources:    sourcesData.sources,   // { [id]: SourceRef }
      nodes:      {},                    // { [id]: Node }
      edges:      {},                    // { [id]: Edge }
      adj:        {},                    // { [id]: Edge[] }  forward
      radj:       {},                    // { [id]: Edge[] }  backward
      byType:     {
        feedstock: [], intermediate: [], material: [],
        application: [], consumer_product: [],
      },
      bySystem:   {},                    // { [system]: nodeId[] }
      byCategory: {},                    // { [category]: nodeId[] }
    };

    // ① nodes
    for (const mod of modules) {
      for (const node of mod.nodes) {
        if (g.nodes[node.id]) {
          console.warn(`ChainGraph: duplicate node id "${node.id}" — skipping`);
          continue;
        }
        g.nodes[node.id]  = { ...node, _system: mod.system, _subsystem: mod.subsystem };
        g.adj[node.id]    = [];
        g.radj[node.id]   = [];

        const t = node.type;
        if (g.byType[t]) g.byType[t].push(node.id);

        const sys = mod.system;
        if (!g.bySystem[sys]) g.bySystem[sys] = [];
        g.bySystem[sys].push(node.id);

        if (t === 'consumer_product' && node.category) {
          if (!g.byCategory[node.category]) g.byCategory[node.category] = [];
          g.byCategory[node.category].push(node.id);
        }
      }
    }

    // ② edges
    for (const mod of modules) {
      for (const edge of mod.edges) {
        if (g.edges[edge.id]) {
          console.warn(`ChainGraph: duplicate edge id "${edge.id}" — skipping`);
          continue;
        }
        if (!g.nodes[edge.from] || !g.nodes[edge.to]) {
          console.warn(`ChainGraph: edge "${edge.id}" references unknown node — skipping`);
          continue;
        }
        g.edges[edge.id] = edge;
        g.adj[edge.from].push(edge);
        g.radj[edge.to].push(edge);
      }
    }

    return Object.freeze(g);
  }

  // ── Query helpers ────────────────────────────────────────────────────────

  /** sourceId → SourceRef | null */
  function resolveSource(graph, sourceId) {
    return graph.sources[sourceId] ?? null;
  }

  /** nodeId の全上流 sourceRef を重複なしで収集 */
  function collectUpstreamSources(graph, nodeId) {
    const seen   = new Set();
    const result = [];
    const stack  = [nodeId];
    while (stack.length) {
      const id   = stack.pop();
      const node = graph.nodes[id];
      if (!node) continue;
      for (const sid of (node.sourceRefs ?? [])) {
        if (!seen.has(sid)) { seen.add(sid); result.push(resolveSource(graph, sid)); }
      }
      for (const edge of (graph.radj[id] ?? [])) {
        for (const sid of (edge.sourceRefs ?? [])) {
          if (!seen.has(sid)) { seen.add(sid); result.push(resolveSource(graph, sid)); }
        }
        stack.push(edge.from);
      }
    }
    return result.filter(Boolean);
  }

  return { load, resolveSource, collectUpstreamSources };
})();
