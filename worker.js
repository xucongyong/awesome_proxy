/**
 * Cloudflare Worker: Automated Proxy Hub & Dashboard
 * Connected to PostgreSQL (proxy.nodes) via Cloudflare Hyperdrive
 */

import { Client } from 'pg';

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (!env.HYPERDRIVE && !env.DB) {
      return new Response("Neither 'HYPERDRIVE' nor 'DB' binding configured.", { status: 500 });
    }

    try {
      if (path === "/sub") {
        return await handleSubscription(request, env, ctx);
      } else if (path === "/json" || path === "/config.json") {
        return await handleSingboxJson(request, env, ctx);
      } else if (path === "/api/stats") {
        return await handleApiStats(request, env, ctx);
      } else if (path === "/api/debug") {
        return await handleDebug(request, env, ctx);
      } else {
        return await handleDashboard(request, env, ctx);
      }
    } catch (err) {
      return new Response(`Proxy Hub Notice: ${err.message}`, {
        status: 200,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      });
    }
  },
};

/**
 * Universal Database Query Helper:
 * Prefers PostgreSQL via Hyperdrive (accelerated pool & edge cache),
 * falls back to D1 if Hyperdrive is not bound.
 */
async function dbQuery(env, ctx, sql, params = []) {
  if (env.HYPERDRIVE) {
    const client = new Client({ connectionString: env.HYPERDRIVE.connectionString });
    await client.connect();
    try {
      const res = await client.query(sql, params);
      return res.rows;
    } finally {
      ctx.waitUntil(client.end());
    }
  }

  // Fallback to legacy D1
  if (env.DB) {
    const stmt = env.DB.prepare(sql);
    const bound = params.length > 0 ? stmt.bind(...params) : stmt;
    const res = await bound.all();
    return res.results || [];
  }

  return [];
}

async function handleDebug(request, env, ctx) {
  if (!env.HYPERDRIVE) return new Response(JSON.stringify({ error: "No HYPERDRIVE" }), { headers: { "Content-Type": "application/json" } });
  const client = new Client({ connectionString: env.HYPERDRIVE.connectionString });
  await client.connect();
  try {
    const meta = await client.query(`SELECT current_database() as database, current_user as user, current_schema as schema;`);
    const tables = await client.query(`SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema');`);
    return new Response(JSON.stringify({ meta: meta.rows[0], tables: tables.rows }, null, 2), { headers: { "Content-Type": "application/json" } });
  } catch (err) {
    return new Response(JSON.stringify({ error: err.message }), { headers: { "Content-Type": "application/json" } });
  } finally {
    ctx.waitUntil(client.end());
  }
}

/**
 * 1. Base64 订阅分发接口 (/sub) - 带边缘缓存保护
 */
async function handleSubscription(request, env, ctx) {
  const cache = caches.default;
  const cacheKey = new Request(request.url, request);
  let cached = await cache.match(cacheKey);
  if (cached) return cached;

  try {
    const sql = `
      SELECT node_url
      FROM proxy.nodes
      WHERE (cn_is_active = 1 OR (cn_is_active IS NULL AND status = 'active'))
        AND COALESCE(cn_delay_ms, delay_ms) > 0
      ORDER BY COALESCE(cn_delay_ms, delay_ms) ASC, speed_mbps DESC
      LIMIT 100;
    `;
    const rows = await dbQuery(env, ctx, sql);
    const urls = rows.map((r) => r.node_url).filter(Boolean);
    const plainText = urls.join("\n");
    const b64 = btoa(unescape(encodeURIComponent(plainText)));

    const resp = new Response(b64, {
      headers: {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "public, max-age=60, s-maxage=60",
        "Access-Control-Allow-Origin": "*",
      },
    });
    ctx.waitUntil(cache.put(cacheKey, resp.clone()));
    return resp;
  } catch (err) {
    return new Response(btoa(""), {
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  }
}

/**
 * 2. API 统计接口 (/api/stats)
 */
async function handleApiStats(request, env, ctx) {
  try {
    const statsQuery = `
      SELECT
        (SELECT COUNT(*) FROM proxy.nodes) AS total,
        (SELECT COUNT(*) FROM proxy.nodes WHERE cn_is_active = 1 OR (cn_is_active IS NULL AND status = 'active')) AS cn_active,
        (SELECT COUNT(*) FROM proxy.nodes WHERE global_is_active = 1) AS global_active,
        (SELECT COUNT(*) FROM proxy.nodes WHERE status = 'dead') AS dead,
        (SELECT COUNT(*) FROM proxy.nodes WHERE status = 'untested') AS untested;
    `;
    const rows = await dbQuery(env, ctx, statsQuery);
    const statRow = rows[0] || {};
    const formatted = {
      total: Number(statRow.total || 0),
      cn_active: Number(statRow.cn_active || 0),
      global_active: Number(statRow.global_active || 0),
      dead: Number(statRow.dead || 0),
      untested: Number(statRow.untested || 0),
    };
    return new Response(JSON.stringify(formatted, null, 2), {
      headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
    });
  } catch (err) {
    return new Response(JSON.stringify({ error: err.message, total: 12810 }), {
      headers: { "Content-Type": "application/json" },
    });
  }
}

/**
 * 3. sing-box 1.14 配置分发接口 (/json)
 */
async function handleSingboxJson(request, env, ctx) {
  const cache = caches.default;
  const cacheKey = new Request(request.url, request);
  let cached = await cache.match(cacheKey);
  if (cached) return cached;

  let nodes = [];
  try {
    const sql = `
      SELECT id, node_url, protocol, COALESCE(cn_delay_ms, delay_ms) as delay_ms, speed_mbps
      FROM proxy.nodes
      WHERE (cn_is_active = 1 OR (cn_is_active IS NULL AND status = 'active'))
        AND COALESCE(cn_delay_ms, delay_ms) > 0
      ORDER BY delay_ms ASC, speed_mbps DESC
      LIMIT 50;
    `;
    nodes = await dbQuery(env, ctx, sql);
  } catch (err) {}

  const outbounds = [];
  const tags = [];

  for (let i = 0; i < nodes.length; i++) {
    const n = nodes[i];
    const delay = Number(n.delay_ms || 0);
    const tag = `node-${i + 1}-${n.protocol}-${delay}ms`;
    const parsed = parseNodeSimple(n.node_url, tag);
    if (parsed) {
      outbounds.push(parsed);
      tags.push(tag);
    }
  }

  if (tags.length === 0) tags.push("direct");

  const config = {
    log: { level: "info", timestamp: true },
    dns: {
      servers: [
        { tag: "remote-dns", type: "udp", server: "1.1.1.1" },
        { tag: "local-dns", type: "udp", server: "223.5.5.5", detour: "direct" },
      ],
    },
    inbounds: [
      { type: "mixed", tag: "mixed-in", listen: "127.0.0.1", listen_port: 7890 },
    ],
    outbounds: [
      {
        type: "selector",
        tag: "select",
        outbounds: ["auto-urltest", ...tags, "direct"],
        default: "auto-urltest",
      },
      {
        type: "urltest",
        tag: "auto-urltest",
        outbounds: tags,
        url: "http://cp.cloudflare.com/generate_204",
        interval: "3m",
        tolerance: 50,
      },
      ...outbounds,
      { type: "direct", tag: "direct" },
      { type: "block", tag: "block" },
    ],
    route: {
      default_domain_resolver: "local-dns",
      rules: [
        { protocol: "dns", action: "hijack-dns" },
        { ip_is_private: true, outbound: "direct" },
      ],
      auto_detect_interface: true,
    },
  };

  const resp = new Response(JSON.stringify(config, null, 2), {
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "public, max-age=60, s-maxage=60",
      "Access-Control-Allow-Origin": "*",
    },
  });
  ctx.waitUntil(cache.put(cacheKey, resp.clone()));
  return resp;
}

function parseNodeSimple(url, tag) {
  try {
    const u = new URL(url);
    const proto = u.protocol.replace(":", "");
    if (proto === "vless") {
      return {
        type: "vless",
        tag,
        server: u.hostname,
        server_port: parseInt(u.port || 443),
        uuid: u.username,
        tls: { enabled: true, server_name: u.hostname },
      };
    } else if (proto === "trojan") {
      return {
        type: "trojan",
        tag,
        server: u.hostname,
        server_port: parseInt(u.port || 443),
        password: u.username,
        tls: { enabled: true, server_name: u.hostname },
      };
    }
  } catch (e) {}
  return null;
}

/**
 * 4. 实时暗黑科技风仪表盘页面 (/)
 */
async function handleDashboard(request, env, ctx) {
  const cache = caches.default;
  const cacheKey = new Request(request.url, request);
  let cached = await cache.match(cacheKey);
  if (cached) return cached;

  let stats = { total: 12810, cn_active: 0, global_active: 0, dead: 0, untested: 12810 };
  let topNodes = [];
  let dbError = null;

  try {
    const statsSql = `
      SELECT
        (SELECT COUNT(*) FROM proxy.nodes) AS total,
        (SELECT COUNT(*) FROM proxy.nodes WHERE cn_is_active = 1 OR (cn_is_active IS NULL AND status = 'active')) AS cn_active,
        (SELECT COUNT(*) FROM proxy.nodes WHERE global_is_active = 1) AS global_active,
        (SELECT COUNT(*) FROM proxy.nodes WHERE status = 'dead') AS dead,
        (SELECT COUNT(*) FROM proxy.nodes WHERE status = 'untested') AS untested;
    `;
    const rows = await dbQuery(env, ctx, statsSql);
    if (rows && rows[0]) {
      stats = {
        total: Number(rows[0].total || 0),
        cn_active: Number(rows[0].cn_active || 0),
        global_active: Number(rows[0].global_active || 0),
        dead: Number(rows[0].dead || 0),
        untested: Number(rows[0].untested || 0),
      };
    }
  } catch (err) {
    dbError = err.message;
  }

  try {
    const topSql = `
      SELECT id, protocol, node_url,
             COALESCE(cn_delay_ms, delay_ms) AS delay_ms,
             speed_mbps, last_tested
      FROM proxy.nodes
      WHERE (cn_is_active = 1 OR (cn_is_active IS NULL AND status = 'active'))
        AND COALESCE(cn_delay_ms, delay_ms) > 0
      ORDER BY delay_ms ASC, speed_mbps DESC
      LIMIT 20;
    `;
    const rows = await dbQuery(env, ctx, topSql);
    if (rows) topNodes = rows;
  } catch (err) {
    if (!dbError) dbError = err.message;
  }

  const origin = new URL(request.url).origin;
  const subUrl = `${origin}/sub`;
  const jsonUrl = `${origin}/json`;

  const rowsHtml = (topNodes || []).map((n, idx) => {
    let host = "unknown";
    try {
      const u = new URL(n.node_url);
      host = `${u.hostname}:${u.port || 443}`;
    } catch(e) {}

    let badge = "bg-green-500/20 text-green-400 border-green-500/30";
    let grade = "⭐⭐⭐⭐⭐ 极优";
    const delay = Number(n.delay_ms || 0);
    if (delay > 600) {
      badge = "bg-amber-500/20 text-amber-400 border-amber-500/30";
      grade = "⭐⭐⭐⭐ 良好";
    }
    if (delay > 1200) {
      badge = "bg-blue-500/20 text-blue-400 border-blue-500/30";
      grade = "⭐⭐⭐ 普通";
    }

    return `
      <tr class="border-b border-gray-800 hover:bg-gray-800/40 transition">
        <td class="py-3 px-4 text-gray-400 font-mono">#${idx + 1}</td>
        <td class="py-3 px-4">
          <span class="px-2 py-0.5 rounded text-xs uppercase font-semibold border ${badge}">
            ${n.protocol}
          </span>
        </td>
        <td class="py-3 px-4 font-mono font-bold text-emerald-400">${delay} ms</td>
        <td class="py-3 px-4 font-mono text-gray-300">${n.speed_mbps ? n.speed_mbps + ' Mbps' : '未抽样'}</td>
        <td class="py-3 px-4 text-sm">${grade}</td>
        <td class="py-3 px-4 font-mono text-xs text-gray-400 truncate max-w-xs">${host}</td>
      </tr>
    `;
  }).join("");

  let noticeBanner = "";
  if (dbError) {
    noticeBanner = `
      <div class="bg-amber-950/40 border border-amber-500/40 rounded-xl p-4 my-4 flex items-start gap-3 text-amber-300 text-xs">
        <span class="text-base">⚠️</span>
        <div class="leading-relaxed">
          <div class="font-bold mb-0.5">PostgreSQL / Hyperdrive 连接提示</div>
          <div>${dbError}</div>
        </div>
      </div>
    `;
  }

  const html = `
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>🌐 全球代理池实时监控大盘</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    function copyText(text, btnId) {
      navigator.clipboard.writeText(text).then(() => {
        const btn = document.getElementById(btnId);
        const orig = btn.innerText;
        btn.innerText = "已复制 ✅";
        setTimeout(() => { btn.innerText = orig; }, 2000);
      });
    }
  </script>
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen antialiased">
  <div class="max-w-6xl mx-auto px-4 py-8">
    <!-- Header -->
    <div class="flex flex-col md:flex-row items-start md:items-center justify-between pb-6 border-b border-gray-800 gap-4">
      <div>
        <h1 class="text-2xl font-bold bg-gradient-to-r from-blue-400 via-teal-300 to-emerald-400 bg-clip-text text-transparent">
          🌐 全球代理池实时大盘
        </h1>
        <p class="text-xs text-gray-400 mt-1">PostgreSQL (proxy.nodes) · Cloudflare Hyperdrive 边缘加速驱动</p>
      </div>
      <div class="flex items-center gap-2">
        <button id="btnSub" onclick="copyText('${subUrl}', 'btnSub')" class="px-3 py-2 bg-blue-600 hover:bg-blue-500 rounded-lg text-xs font-semibold shadow transition">
          复制通用订阅 (Base64)
        </button>
        <button id="btnJson" onclick="copyText('${jsonUrl}', 'btnJson')" class="px-3 py-2 bg-emerald-600 hover:bg-emerald-500 rounded-lg text-xs font-semibold shadow transition">
          复制 sing-box 1.14 配置
        </button>
      </div>
    </div>

    ${noticeBanner}

    <!-- Stats Grid -->
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4 my-6">
      <div class="bg-gray-900/80 border border-gray-800 rounded-xl p-4">
        <div class="text-xs text-gray-400">国内可用节点 (CN Active)</div>
        <div class="text-3xl font-black text-emerald-400 mt-1 font-mono">${stats.cn_active || 0}</div>
        <div class="text-[10px] text-emerald-500/80 mt-1">● 客户端直连即用</div>
      </div>
      <div class="bg-gray-900/80 border border-gray-800 rounded-xl p-4">
        <div class="text-xs text-gray-400">海外存活节点 (Global Active)</div>
        <div class="text-3xl font-black text-blue-400 mt-1 font-mono">${stats.global_active || 0}</div>
        <div class="text-[10px] text-blue-400/80 mt-1">● 服务端存活 / 备用池</div>
      </div>
      <div class="bg-gray-900/80 border border-gray-800 rounded-xl p-4">
        <div class="text-xs text-gray-400">历史总沉淀 (Total Nodes)</div>
        <div class="text-3xl font-black text-purple-400 mt-1 font-mono">${stats.total || 0}</div>
        <div class="text-[10px] text-purple-400/80 mt-1">● 全网唯一指纹去重</div>
      </div>
      <div class="bg-gray-900/80 border border-gray-800 rounded-xl p-4">
        <div class="text-xs text-gray-400">已淘汰失效 (Dead Nodes)</div>
        <div class="text-3xl font-black text-gray-500 mt-1 font-mono">${stats.dead || 0}</div>
        <div class="text-[10px] text-gray-500/80 mt-1">● 软下线拦截</div>
      </div>
    </div>

    <!-- Active Leaderboard -->
    <div class="bg-gray-900/80 border border-gray-800 rounded-xl overflow-hidden shadow-2xl">
      <div class="px-6 py-4 border-b border-gray-800 flex items-center justify-between">
        <h2 class="text-base font-semibold text-gray-200">🏆 TOP 20 国内直连最优质节点</h2>
        <span class="text-xs text-gray-500">按延迟升序 · 毫秒级排序</span>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left border-collapse">
          <thead>
            <tr class="bg-gray-950/60 text-[11px] text-gray-400 uppercase tracking-wider border-b border-gray-800">
              <th class="py-3 px-4">排名</th>
              <th class="py-3 px-4">协议</th>
              <th class="py-3 px-4">国内延迟</th>
              <th class="py-3 px-4">实测带宽</th>
              <th class="py-3 px-4">质量评级</th>
              <th class="py-3 px-4">服务器地址</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-800/40 text-sm">
            ${rowsHtml || '<tr><td colspan="6" class="py-8 text-center text-gray-500">暂无测速存活节点，请等待国内定时任务运行。</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Footer -->
    <div class="mt-8 text-center text-xs text-gray-600">
      由 Cloudflare Worker & Hyperdrive (PostgreSQL) 边缘驱动 · 零服务器开销 · 智能边缘缓存保护
    </div>
  </div>
</body>
</html>
  `;

  const resp = new Response(html, {
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "public, max-age=60, s-maxage=60",
    },
  });
  ctx.waitUntil(cache.put(cacheKey, resp.clone()));
  return resp;
}
