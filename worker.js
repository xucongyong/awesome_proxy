/**
 * Cloudflare Worker: Automated Proxy Hub & Dashboard
 * Directly binds to Cloudflare D1 (nodes-db: 3b55a783-16d9-4f31-bd9f-312972752269)
 */

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    // Safety check for D1 database binding
    if (!env.DB) {
      return new Response("Cloudflare D1 binding 'DB' not configured.", { status: 500 });
    }

    try {
      if (path === "/sub") {
        return handleSubscription(request, env);
      } else if (path === "/json" || path === "/config.json") {
        return handleSingboxJson(request, env);
      } else if (path === "/api/stats") {
        return handleApiStats(request, env);
      } else {
        return handleDashboard(request, env);
      }
    } catch (err) {
      return new Response(`Server Error: ${err.message}`, { status: 500 });
    }
  },
};

/**
 * 1. Base64 订阅分发接口 (/sub)
 */
async function handleSubscription(request, env) {
  const sql = `
    SELECT node_url
    FROM nodes
    WHERE (cn_is_active = 1 OR (cn_is_active IS NULL AND status = 'active'))
      AND COALESCE(cn_delay_ms, delay_ms) > 0
    ORDER BY COALESCE(cn_delay_ms, delay_ms) ASC, speed_mbps DESC
    LIMIT 100;
  `;
  const { results } = await env.DB.prepare(sql).all();
  const urls = (results || []).map(r => r.node_url).filter(Boolean);
  const plainText = urls.join("\n");
  const b64 = btoa(unescape(encodeURIComponent(plainText)));

  return new Response(b64, {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "no-cache, no-store, must-revalidate",
      "Access-Control-Allow-Origin": "*",
    },
  });
}

/**
 * 2. API 统计接口 (/api/stats)
 */
async function handleApiStats(request, env) {
  const statsQuery = `
    SELECT
      (SELECT COUNT(*) FROM nodes) AS total,
      (SELECT COUNT(*) FROM nodes WHERE cn_is_active = 1 OR (cn_is_active IS NULL AND status = 'active')) AS cn_active,
      (SELECT COUNT(*) FROM nodes WHERE global_is_active = 1) AS global_active,
      (SELECT COUNT(*) FROM nodes WHERE status = 'dead') AS dead,
      (SELECT COUNT(*) FROM nodes WHERE status = 'untested') AS untested;
  `;
  const statRow = await env.DB.prepare(statsQuery).first();
  return new Response(JSON.stringify(statRow || {}, null, 2), {
    headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
  });
}

/**
 * 3. sing-box 1.14 配置分发接口 (/json)
 */
async function handleSingboxJson(request, env) {
  const sql = `
    SELECT id, node_url, protocol, COALESCE(cn_delay_ms, delay_ms) as delay_ms, speed_mbps
    FROM nodes
    WHERE (cn_is_active = 1 OR (cn_is_active IS NULL AND status = 'active'))
      AND COALESCE(cn_delay_ms, delay_ms) > 0
    ORDER BY delay_ms ASC, speed_mbps DESC
    LIMIT 50;
  `;
  const { results } = await env.DB.prepare(sql).all();
  const nodes = results || [];

  const outbounds = [];
  const tags = [];

  for (let i = 0; i < nodes.length; i++) {
    const n = nodes[i];
    const tag = `node-${i + 1}-${n.protocol}-${n.delay_ms}ms`;
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

  return new Response(JSON.stringify(config, null, 2), {
    headers: { "Content-Type": "application/json; charset=utf-8", "Access-Control-Allow-Origin": "*" },
  });
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
async function handleDashboard(request, env) {
  const statsSql = `
    SELECT
      (SELECT COUNT(*) FROM nodes) AS total,
      (SELECT COUNT(*) FROM nodes WHERE cn_is_active = 1 OR (cn_is_active IS NULL AND status = 'active')) AS cn_active,
      (SELECT COUNT(*) FROM nodes WHERE global_is_active = 1) AS global_active,
      (SELECT COUNT(*) FROM nodes WHERE status = 'dead') AS dead,
      (SELECT COUNT(*) FROM nodes WHERE status = 'untested') AS untested;
  `;
  const stats = (await env.DB.prepare(statsSql).first()) || {};

  const topSql = `
    SELECT id, protocol, node_url,
           COALESCE(cn_delay_ms, delay_ms) AS delay_ms,
           speed_mbps, last_tested
    FROM nodes
    WHERE (cn_is_active = 1 OR (cn_is_active IS NULL AND status = 'active'))
      AND COALESCE(cn_delay_ms, delay_ms) > 0
    ORDER BY delay_ms ASC, speed_mbps DESC
    LIMIT 20;
  `;
  const { results: topNodes } = await env.DB.prepare(topSql).all();

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
    if (n.delay_ms > 600) {
      badge = "bg-amber-500/20 text-amber-400 border-amber-500/30";
      grade = "⭐⭐⭐⭐ 良好";
    }
    if (n.delay_ms > 1200) {
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
        <td class="py-3 px-4 font-mono font-bold text-emerald-400">${n.delay_ms} ms</td>
        <td class="py-3 px-4 font-mono text-gray-300">${n.speed_mbps ? n.speed_mbps + ' Mbps' : '未抽样'}</td>
        <td class="py-3 px-4 text-sm">${grade}</td>
        <td class="py-3 px-4 font-mono text-xs text-gray-400 truncate max-w-xs">${host}</td>
      </tr>
    `;
  }).join("");

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
          🌐 全球代理池实时大盘 (Cloudflare D1)
        </h1>
        <p class="text-xs text-gray-400 mt-1">海外粗筛 + 国内精筛 · 24小时全自动清洗池</p>
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

    <!-- Stats Grid -->
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4 my-8">
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
      由 Cloudflare Worker & D1 全球边缘驱动 · 零服务器开销 · 实时读写
    </div>
  </div>
</body>
</html>
  `;

  return new Response(html, {
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}
