'use strict';

/**
 * chain-graph.js — グラフデータ読み込み・マージ・クエリ・バリデーション層
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
    'data/chains/agriculture.json',
  ];

  // ── 有効な type 間接続ルール ─────────────────────────────────────────────
  // key = from.type, value = Set of allowed to.type
  const VALID_TYPE_CONNECTIONS = {
    feedstock:        new Set(['feedstock', 'intermediate', 'material']),
    intermediate:     new Set(['intermediate', 'material']),
    material:         new Set(['application']),
    application:      new Set(['consumer_product']),
    consumer_product: new Set(),  // consumer_product は出力エッジを持たない
  };

  // ── Load ────────────────────────────────────────────────────────────────

  /**
   * 全チェーンファイルを並列取得してマージしたグラフを返す
   * バリデーションを実行し、エラーがあればコンソールに警告する
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
    const graph = _build(sourcesData, modules);

    // バリデーション実行・コンソール出力（エラーがあっても続行）
    const vr = validate(graph);
    if (!vr.valid) {
      console.error('ChainGraph validate: errors found', vr.errors);
    }
    if (vr.warnings.length > 0) {
      console.warn('ChainGraph validate: warnings', vr.warnings);
    }

    return graph;
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

  // ── Validate ─────────────────────────────────────────────────────────────

  /**
   * グラフの整合性を検証する
   *
   * チェック項目:
   *   ERROR: 不正な type 間接続
   *   ERROR: consumer_product に到達できないノード（feedstock 起点の BFS）
   *   WARNING: 孤立ノード（入次数=0 かつ 出次数=0 かつ feedstock でない）
   *   WARNING: sourceRef が未設定の edge
   *   WARNING: 未知の sourceRef ID を持つノード/エッジ
   *
   * @param {Graph} graph
   * @returns {{ valid: boolean, errors: string[], warnings: string[] }}
   */
  function validate(graph) {
    const errors   = [];
    const warnings = [];
    const knownSrcIds = new Set(Object.keys(graph.sources ?? {}));

    // ① 不正 type 間接続チェック
    for (const [eid, edge] of Object.entries(graph.edges)) {
      const fromType = graph.nodes[edge.from]?.type;
      const toType   = graph.nodes[edge.to]?.type;
      if (!fromType || !toType) continue; // already warned in _build
      const allowed = VALID_TYPE_CONNECTIONS[fromType];
      if (allowed !== undefined && !allowed.has(toType)) {
        errors.push(
          `INVALID_CONNECTION: edge "${eid}" ${fromType}(${edge.from}) → ${toType}(${edge.to}) は許可されていない接続です`
        );
      }
    }

    // ② feedstock 起点から BFS して到達可能セットを計算
    const reachable = new Set();
    const queue = [...(graph.byType.feedstock ?? [])];
    while (queue.length > 0) {
      const id = queue.shift();
      if (reachable.has(id)) continue;
      reachable.add(id);
      for (const edge of (graph.adj[id] ?? [])) {
        if (!reachable.has(edge.to)) queue.push(edge.to);
      }
    }

    // consumer_product の中で到達不能なものをエラー
    for (const id of (graph.byType.consumer_product ?? [])) {
      if (!reachable.has(id)) {
        errors.push(
          `UNREACHABLE_CONSUMER: consumer_product "${id}" はどの feedstock からも到達できません`
        );
      }
    }

    // ③ 孤立ノードチェック（feedstock 以外で入次数=0 かつ 出次数=0）
    for (const [id, node] of Object.entries(graph.nodes)) {
      if (node.type === 'feedstock') continue;
      const hasIn  = (graph.radj[id] ?? []).length > 0;
      const hasOut = (graph.adj[id]  ?? []).length > 0;
      if (!hasIn && !hasOut) {
        warnings.push(`ORPHAN_NODE: "${id}" (${node.type}) は入辺も出辺もありません`);
      }
    }

    // ④ edge の sourceRefs 欠落チェック
    for (const [eid, edge] of Object.entries(graph.edges)) {
      if (!edge.sourceRefs || edge.sourceRefs.length === 0) {
        warnings.push(`MISSING_EDGE_SOURCEREFS: edge "${eid}" に sourceRefs が設定されていません`);
      }
    }

    // ⑤ 未知の sourceRef ID チェック（ノード・エッジ両方）
    for (const [id, node] of Object.entries(graph.nodes)) {
      for (const sid of (node.sourceRefs ?? [])) {
        if (!knownSrcIds.has(sid)) {
          warnings.push(`UNKNOWN_SOURCEREF: node "${id}" が未知の sourceRef "${sid}" を参照しています`);
        }
      }
    }
    for (const [eid, edge] of Object.entries(graph.edges)) {
      for (const sid of (edge.sourceRefs ?? [])) {
        if (!knownSrcIds.has(sid)) {
          warnings.push(`UNKNOWN_SOURCEREF: edge "${eid}" が未知の sourceRef "${sid}" を参照しています`);
        }
      }
    }

    return { valid: errors.length === 0, errors, warnings };
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

  /**
   * パス上の evidenceTier を集約して最低ランクを返す
   * primary > secondary > proxy の順で信頼度が下がる
   * @param {Graph} graph
   * @param {{ node, edge }[]} path - getCriticalPath() の戻り値
   * @returns {"primary"|"secondary"|"proxy"}
   */
  function getPathEvidenceTier(graph, path) {
    const TIER_RANK = { primary: 0, secondary: 1, proxy: 2 };
    let worst = 0; // 0=primary が最良

    for (const step of path) {
      // ノードの sourceRefs
      for (const sid of (step.node?.sourceRefs ?? [])) {
        const src = resolveSource(graph, sid);
        if (src?.evidenceTier) {
          const rank = TIER_RANK[src.evidenceTier] ?? 2;
          if (rank > worst) worst = rank;
        }
      }
      // エッジの sourceRefs
      for (const sid of (step.edge?.sourceRefs ?? [])) {
        const src = resolveSource(graph, sid);
        if (src?.evidenceTier) {
          const rank = TIER_RANK[src.evidenceTier] ?? 2;
          if (rank > worst) worst = rank;
        }
      }
    }

    const TIER_NAMES = ['primary', 'secondary', 'proxy'];
    return TIER_NAMES[worst];
  }

  return { load, validate, resolveSource, collectUpstreamSources, getPathEvidenceTier };
})();
