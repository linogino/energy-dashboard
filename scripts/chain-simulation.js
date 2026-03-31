'use strict';

/**
 * chain-simulation.js — impact propagation & critical path
 *
 * UI 非依存。DOM 操作・fetch は一切含まない。
 * 入力: graph (ChainGraph.load() の戻り値), scenario
 * 出力: impactMap / ranked list / path array
 */
const ChainSimulation = (() => {

  // ── Scenarios ────────────────────────────────────────────────────────────

  /**
   * stockLevel: 0.0 = 完全停止 / 1.0 = 平常
   * 指定していないノードは disruption なし（stockLevel = 1.0）
   */
  const SCENARIOS = {
    hormuz_block: {
      id:          'hormuz_block',
      label:       'ホルムズ封鎖継続（現在）',
      description: 'ホルムズ海峡通航 -97%。ナフサ・原油輸入が大幅減少。',
      stockLevels: { naphtha: 0.25, ethylene: 0.45 },
    },
    naphtha_30pct: {
      id:          'naphtha_30pct',
      label:       'ナフサ在庫30%',
      description: 'ナフサ在庫が平常時の30%まで低下した場合。',
      stockLevels: { naphtha: 0.30 },
    },
    normal: {
      id:          'normal',
      label:       '平常時',
      description: '供給途絶なし。',
      stockLevels: {},
    },
  };

  // ── Impact Propagation ───────────────────────────────────────────────────

  /**
   * Kahn's algorithm でトポロジカル順に BFS し、
   * 各ノードの supplyRisk を伝播させる。
   *
   * supplyRisk[child] = max(supplyRisk[parent]) × (1 − child.substitutability)
   *
   * @param {Graph}    graph
   * @param {Scenario} scenario
   * @returns {{ [nodeId]: { supplyRisk: number, impactScore: number } }}
   */
  function propagateImpact(graph, scenario) {
    const impact = {};
    for (const id of Object.keys(graph.nodes)) {
      impact[id] = { supplyRisk: 0, impactScore: 0 };
    }

    // シナリオ初期リスクを設定
    for (const [id, stock] of Object.entries(scenario.stockLevels ?? {})) {
      if (impact[id]) impact[id].supplyRisk = _clamp01(1 - stock);
    }

    // Kahn's in-degree カウント
    const inDeg = {};
    for (const id of Object.keys(graph.nodes)) inDeg[id] = 0;
    for (const e of Object.values(graph.edges)) inDeg[e.to]++;

    const queue = Object.keys(graph.nodes).filter(id => inDeg[id] === 0);
    const visited = new Set();

    while (queue.length > 0) {
      const id = queue.shift();
      if (visited.has(id)) continue;
      visited.add(id);

      const node  = graph.nodes[id];
      const subst = node.scores?.substitutability?.value ?? 0.5;

      // 親からのリスク伝播
      const parentEdges = graph.radj[id] ?? [];
      if (parentEdges.length > 0) {
        const maxParentRisk = Math.max(
          ...parentEdges.map(e => impact[e.from]?.supplyRisk ?? 0)
        );
        const inherited = maxParentRisk * (1 - subst);
        impact[id].supplyRisk = _clamp01(Math.max(impact[id].supplyRisk, inherited));
      }

      // consumer_product のみ impactScore を計算
      if (node.type === 'consumer_product') {
        const rel = node.scores?.consumerRelevance?.value ?? 50;
        impact[id].impactScore = Math.round(impact[id].supplyRisk * (rel / 100) * 100);
      }

      // 子ノードをキューへ
      for (const edge of (graph.adj[id] ?? [])) {
        inDeg[edge.to]--;
        if (inDeg[edge.to] <= 0 && !visited.has(edge.to)) {
          queue.push(edge.to);
        }
      }
    }

    return impact;
  }

  // ── Rank Consumer Products ───────────────────────────────────────────────

  /**
   * consumer_product を impactScore 降順でランク付け
   * @param {string|null} systemFilter - 系統フィルタ ("ethylene" 等)。null = 全て
   * @returns {{ node, supplyRisk, impactScore }[]}
   */
  function rankConsumerProducts(graph, impactMap, systemFilter = null) {
    let ids = graph.byType.consumer_product ?? [];

    if (systemFilter) {
      const sysSet = new Set(graph.bySystem[systemFilter] ?? []);
      ids = ids.filter(id => sysSet.has(id));
    }

    return ids
      .map(id => ({
        node:        graph.nodes[id],
        supplyRisk:  impactMap[id]?.supplyRisk  ?? 0,
        impactScore: impactMap[id]?.impactScore ?? 0,
      }))
      .sort((a, b) => b.impactScore - a.impactScore);
  }

  // ── Critical Path ────────────────────────────────────────────────────────

  /**
   * consumer_product → feedstock 方向に逆向き BFS し、
   * 最も「代替困難」な経路（edge weight = 1 − parent.substitutability が大きい経路）
   * を返す。
   *
   * @returns {{ node: Node, edge: Edge|null }[]}  feedstock → product 順
   */
  function getCriticalPath(graph, consumerProductId) {
    if (!graph.nodes[consumerProductId]) return [];

    const dist     = {};   // nodeId → 累積 weight
    const prev     = {};   // nodeId → 親 nodeId
    const prevEdge = {};   // nodeId → 使用した edge

    for (const id of Object.keys(graph.nodes)) dist[id] = -Infinity;
    dist[consumerProductId] = 0;

    const visited = new Set();
    const queue   = [consumerProductId];

    while (queue.length > 0) {
      const id = queue.shift();
      if (visited.has(id)) continue;
      visited.add(id);

      for (const edge of (graph.radj[id] ?? [])) {
        const parent  = graph.nodes[edge.from];
        const subst   = parent.scores?.substitutability?.value ?? 0.5;
        const weight  = 1 - subst;          // 代替困難ほど重い
        const newDist = dist[id] + weight;

        if (newDist > dist[edge.from]) {
          dist[edge.from] = newDist;
          prev[edge.from]     = id;
          prevEdge[edge.from] = edge;
          if (!visited.has(edge.from)) queue.push(edge.from);
        }
      }
    }

    // 到達可能な feedstock の中で最も距離が大きいものを起点とする
    const reachable = (graph.byType.feedstock ?? []).filter(id => dist[id] > -Infinity);
    if (reachable.length === 0) return [];

    const startId = reachable.reduce((best, id) => dist[id] > dist[best] ? id : best, reachable[0]);

    // パス再構築（feedstock → consumer_product 順）
    const reversed = [];
    let cur = startId;
    while (cur !== undefined) {
      reversed.push({ node: graph.nodes[cur], edge: prevEdge[cur] ?? null });
      cur = prev[cur];
    }
    return reversed.reverse();
  }

  // ── Utils ────────────────────────────────────────────────────────────────

  function _clamp01(v) { return Math.max(0, Math.min(1, v)); }

  // ── Public API ───────────────────────────────────────────────────────────

  return {
    SCENARIOS,
    propagateImpact,
    rankConsumerProducts,
    getCriticalPath,
  };
})();
