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
  /**
   * 共通前提: urea_supply = 0.5 （尿素制約がディーゼル物流にグローバルに影響）
   *
   * 各シナリオが定義するのは上流制約のみ:
   *   - naphtha_availability : ナフサ供給率 (0〜1)
   *   - lng_availability     : LNG供給率   (0〜1)
   *   - substitution_ratio   : 喪失分代替調達率
   *
   * エチレン・プロピレン・BTX以降の下流値は propagateImpact() が自動導出する。
   */
  const SCENARIOS = {
    best_case_partial_recovery: {
      id:          'best_case_partial_recovery',
      label:       'ベストケース：ホルムズ部分回復',
      description: 'ホルムズ海峡が部分的に回復。代替ルート確保により供給危機は緩和。',
      upstream: {
        naphtha_availability:  0.75,
        lng_availability:      0.85,
        substitution_ratio:    0.75,
      },
      stockLevels: {
        naphtha:     0.75,
        natural_gas: 0.85,
        urea:        0.50,
      },
    },

    middle_case_hormuz_loss_60pct_replacement: {
      id:          'middle_case_hormuz_loss_60pct_replacement',
      label:       'ミドルケース：代替調達60%確保',
      description: 'ホルムズ封鎖継続。代替ルートで喪失分の60%を補填。',
      upstream: {
        naphtha_availability:  0.60,
        lng_availability:      0.70,
        substitution_ratio:    0.60,
      },
      stockLevels: {
        naphtha:     0.60,
        natural_gas: 0.70,
        urea:        0.50,
      },
    },

    worst_case_hormuz_loss_20pct_replacement: {
      id:          'worst_case_hormuz_loss_20pct_replacement',
      label:       'ワーストケース：代替調達20%のみ',
      description: 'ホルムズ封鎖継続。代替調達難航、喪失分の20%しか補填できない。',
      upstream: {
        naphtha_availability:  0.20,
        lng_availability:      0.50,
        substitution_ratio:    0.20,
      },
      stockLevels: {
        naphtha:     0.20,
        natural_gas: 0.50,
        urea:        0.50,
      },
    },
  };

  // ── Confidence helpers ───────────────────────────────────────────────────

  /** confidence 文字列 → 係数 */
  const CONFIDENCE_FACTOR = { high: 1.0, medium: 0.8, low: 0.5 };

  /**
   * ノードの最低 confidence を返す（scores の全フィールドの最小値）
   * @param {Node} node
   * @returns {"high"|"medium"|"low"}
   */
  function _nodeMinConfidence(node) {
    const RANK = { high: 0, medium: 1, low: 2 };
    let worst = 0;
    for (const sc of Object.values(node.scores ?? {})) {
      const r = RANK[sc.confidence] ?? 2;
      if (r > worst) worst = r;
    }
    return ['high', 'medium', 'low'][worst];
  }

  /**
   * パス全体の最低 confidence を返す
   * @param {{ node, edge }[]} path
   * @returns {"high"|"medium"|"low"}
   */
  function getPathConfidence(path) {
    const RANK = { high: 0, medium: 1, low: 2 };
    let worst = 0;
    for (const step of path) {
      const r = RANK[_nodeMinConfidence(step.node)] ?? 2;
      if (r > worst) worst = r;
    }
    return ['high', 'medium', 'low'][worst];
  }

  // ── Impact Propagation ───────────────────────────────────────────────────

  /**
   * Kahn's algorithm でトポロジカル順に BFS し、
   * 各ノードの supplyRisk を伝播させる。
   *
   * supplyRisk[child] = max(supplyRisk[parent]) × (1 − child.substitutability)
   *
   * confidenceAdjustedScore = impactScore × confidenceFactor
   *   (high=1.0, medium=0.8, low=0.5)
   *
   * @param {Graph}    graph
   * @param {Scenario} scenario
   * @returns {{ [nodeId]: { supplyRisk, impactScore, confidenceAdjustedScore, confidence } }}
   */
  function propagateImpact(graph, scenario) {
    const impact = {};
    for (const id of Object.keys(graph.nodes)) {
      impact[id] = { supplyRisk: 0, impactScore: 0, confidenceAdjustedScore: 0, confidence: 'high' };
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
      const conf  = _nodeMinConfidence(node);
      impact[id].confidence = conf;

      // 親からのリスク伝播
      const parentEdges = graph.radj[id] ?? [];
      if (parentEdges.length > 0) {
        const maxParentRisk = Math.max(
          ...parentEdges.map(e => impact[e.from]?.supplyRisk ?? 0)
        );
        const inherited = maxParentRisk * (1 - subst);
        impact[id].supplyRisk = _clamp01(Math.max(impact[id].supplyRisk, inherited));

        // 親の confidence も伝播（最低値を継承）
        const RANK = { high: 0, medium: 1, low: 2 };
        const NAMES = ['high', 'medium', 'low'];
        const parentWorst = Math.max(
          ...parentEdges.map(e => RANK[impact[e.from]?.confidence] ?? 0)
        );
        const selfRank = RANK[conf] ?? 0;
        impact[id].confidence = NAMES[Math.max(parentWorst, selfRank)];
      }

      // consumer_product のみ impactScore を計算
      if (node.type === 'consumer_product') {
        const rel = node.scores?.consumerRelevance?.value ?? 50;
        impact[id].impactScore = Math.round(impact[id].supplyRisk * (rel / 100) * 100);
        const factor = CONFIDENCE_FACTOR[impact[id].confidence] ?? 0.5;
        impact[id].confidenceAdjustedScore = Math.round(impact[id].impactScore * factor);
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
   * @returns {{ node, supplyRisk, impactScore, confidenceAdjustedScore, confidence }[]}\
   */
  function rankConsumerProducts(graph, impactMap, systemFilter = null) {
    let ids = graph.byType.consumer_product ?? [];

    if (systemFilter) {
      const sysSet = new Set(graph.bySystem[systemFilter] ?? []);
      ids = ids.filter(id => sysSet.has(id));
    }

    return ids
      .map(id => ({
        node:                    graph.nodes[id],
        supplyRisk:              impactMap[id]?.supplyRisk              ?? 0,
        impactScore:             impactMap[id]?.impactScore             ?? 0,
        confidenceAdjustedScore: impactMap[id]?.confidenceAdjustedScore ?? 0,
        confidence:              impactMap[id]?.confidence              ?? 'high',
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

  // ── Path Explanation ─────────────────────────────────────────────────────

  /**
   * クリティカルパスの自然言語説明を生成する
   *
   * 例: "ナフサ → エチレン → EO → MEG → PET樹脂 を通じて 飲料ボトル の供給に影響します。"
   *
   * @param {{ node, edge }[]} path - getCriticalPath() の戻り値（feedstock→product順）
   * @param {{ [nodeId]: { supplyRisk, confidence } }} impactMap
   * @returns {string}
   */
  function generatePathExplanation(path, impactMap) {
    if (path.length === 0) return '経路情報がありません。';

    const names = path.map(step => step.node?.label ?? step.node?.id ?? '?');
    const product = names[names.length - 1];
    const chain   = names.slice(0, -1).join(' → ');

    const productId = path[path.length - 1]?.node?.id;
    const risk = impactMap?.[productId]?.supplyRisk ?? 0;
    const conf = impactMap?.[productId]?.confidence ?? 'high';

    const riskPct  = Math.round(risk * 100);
    const riskText = riskPct >= 60 ? '大きな影響' : riskPct >= 30 ? '中程度の影響' : '軽微な影響';
    const confText = conf === 'low' ? '（推定精度：低）' : conf === 'medium' ? '（推定精度：中）' : '';

    return `${chain} を通じて ${product} の供給に${riskText}が生じる可能性があります${confText}。`;
  }

  // ── Category Aggregation ─────────────────────────────────────────────────

  /**
   * consumer_product を category ごとに集約し、
   * カテゴリ平均 impactScore でソートして返す
   *
   * @returns {{ category, label, avgImpact, maxImpact, count, products }[]}
   */
  function aggregateByCategory(graph, impactMap) {
    const CATEGORY_LABELS = {
      food_beverage:    '食品・飲料',
      personal_care:    '日用品・衛生',
      healthcare:       '医療・医薬',
      infrastructure:   '生活インフラ',
      clothing_textile: '衣料・繊維',
      home_living:      '住まい・生活用品',
    };

    const groups = {};

    for (const id of (graph.byType.consumer_product ?? [])) {
      const node = graph.nodes[id];
      const cat  = node.category ?? 'other';
      if (!groups[cat]) groups[cat] = { category: cat, label: CATEGORY_LABELS[cat] ?? cat, products: [] };
      groups[cat].products.push({
        node,
        impactScore:             impactMap[id]?.impactScore             ?? 0,
        confidenceAdjustedScore: impactMap[id]?.confidenceAdjustedScore ?? 0,
        supplyRisk:              impactMap[id]?.supplyRisk              ?? 0,
        confidence:              impactMap[id]?.confidence              ?? 'high',
      });
    }

    return Object.values(groups).map(g => {
      const scores = g.products.map(p => p.impactScore);
      g.avgImpact = scores.length ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : 0;
      g.maxImpact = scores.length ? Math.max(...scores) : 0;
      g.count     = scores.length;
      return g;
    }).sort((a, b) => b.avgImpact - a.avgImpact);
  }

  // ── Utils ────────────────────────────────────────────────────────────────

  function _clamp01(v) { return Math.max(0, Math.min(1, v)); }

  // ── Public API ───────────────────────────────────────────────────────────

  return {
    SCENARIOS,
    propagateImpact,
    rankConsumerProducts,
    getCriticalPath,
    getPathConfidence,
    generatePathExplanation,
    aggregateByCategory,
  };
})();
