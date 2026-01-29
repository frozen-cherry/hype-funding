#!/usr/bin/env python3
"""
Hyperliquid 全交易对费率追踪器
- 获取所有 Perp 交易对的历史资金费率
- 生成可搜索、可排序的 HTML 报告
"""

import requests
import json
import argparse
from datetime import datetime, timedelta
from typing import Optional
import webbrowser
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

API_URL = "https://api.hyperliquid.xyz/info"

def get_all_perp_assets(include_main_perp=False):
    """获取所有永续合约资产列表"""
    print("📋 获取资产列表...")
    
    main_assets = []
    main_market_data = {}
    
    if include_main_perp:
        # 获取主 perp dex 资产和 Volume/OI 数据
        print("   正在获取主 Perp 资产列表...")
        try:
            resp = requests.post(API_URL, json={"type": "metaAndAssetCtxs"}, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) >= 2:
                    meta = data[0]
                    asset_ctxs = data[1]
                    
                    for asset, ctx in zip(meta['universe'], asset_ctxs):
                        coin_name = asset['name']
                        main_assets.append(coin_name)
                        main_market_data[coin_name] = {
                            'volume24h': float(ctx.get('dayNtlVlm', 0)),
                            'openInterest': float(ctx.get('openInterest', 0)),
                            'markPx': float(ctx.get('markPx', 0)),
                            'funding': float(ctx.get('funding', 0))
                        }
                    
                    print(f"   主 Perp Dex: {len(main_assets)} 个资产")
        except Exception as e:
            print(f"   获取主 Perp 列表失败: {e}")
    else:
        print(f"   主 Perp Dex: [已跳过] (使用 --main-perp 参数开启)")
    
    # 动态获取 HIP-3 (TradFi) 资产列表和 Volume/OI 数据
    print("   正在获取 HIP-3 资产列表...")
    hip3_assets = []
    hip3_market_data = {}
    
    try:
        resp = requests.post(API_URL, json={"type": "metaAndAssetCtxs", "dex": "xyz"}, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) >= 2:
                meta = data[0]
                asset_ctxs = data[1]
                
                for asset, ctx in zip(meta['universe'], asset_ctxs):
                    coin_name = asset['name']
                    hip3_assets.append(coin_name)
                    hip3_market_data[coin_name] = {
                        'volume24h': float(ctx.get('dayNtlVlm', 0)),
                        'openInterest': float(ctx.get('openInterest', 0)),
                        'markPx': float(ctx.get('markPx', 0)),
                        'funding': float(ctx.get('funding', 0))
                    }
                
                print(f"   HIP-3 TradFi: {len(hip3_assets)} 个资产")
    except Exception as e:
        print(f"   获取 HIP-3 列表失败: {e}")
    
    # 合并市场数据
    all_market_data = {**main_market_data, **hip3_market_data}
    
    return main_assets, hip3_assets, all_market_data


def fetch_funding_history(coin: str, start_time: int, end_time: Optional[int] = None) -> list:
    """获取指定币种的历史资金费率（自动分页，支持速率限制重试）"""
    all_data = []
    current_start = start_time
    max_iterations = 20
    
    for iteration in range(max_iterations):
        payload = {
            "type": "fundingHistory",
            "coin": coin,
            "startTime": current_start
        }
        if end_time:
            payload["endTime"] = end_time
        
        # 重试逻辑（处理 429 速率限制）
        max_retries = 3
        for retry in range(max_retries):
            try:
                response = requests.post(API_URL, json=payload, timeout=15)
                
                if response.status_code == 429:
                    # 速率限制，等待后重试
                    wait_time = (retry + 1) * 2  # 2, 4, 6 秒
                    time.sleep(wait_time)
                    continue
                
                if response.status_code != 200:
                    return all_data  # 其他错误，返回已有数据
                
                data = response.json()
                if not data or not isinstance(data, list):
                    return all_data
                
                all_data.extend(data)
                
                if len(data) < 500:
                    return all_data
                
                last_time = max(d['time'] for d in data)
                current_start = last_time + 1
                break  # 成功，跳出重试循环
                
            except Exception:
                if retry == max_retries - 1:
                    return all_data
                time.sleep(1)
        else:
            # 所有重试都失败
            return all_data
    
    return all_data


def fetch_coin_data(coin: str, start_time: int, end_time: int):
    """获取单个币种数据（用于并行）"""
    try:
        history = fetch_funding_history(coin, start_time, end_time)
        if history:
            stats = calculate_stats(history)
            return coin, {'history': history, 'stats': stats}
    except:
        pass
    return coin, {'history': [], 'stats': None}


def calculate_stats(data: list) -> dict:
    """计算费率统计数据"""
    if not data:
        return None
    
    now = datetime.now().timestamp() * 1000
    sorted_data = sorted(data, key=lambda x: x['time'], reverse=True)
    
    def sum_hours(hours):
        cutoff = now - hours * 60 * 60 * 1000
        return sum(float(d['fundingRate']) for d in sorted_data if d['time'] >= cutoff)
    
    rates = [float(d['fundingRate']) for d in data]
    
    return {
        'rate8h': float(sorted_data[0]['fundingRate']) if sorted_data else 0,
        'sum1d': sum_hours(24),
        'sum3d': sum_hours(72),
        'sum7d': sum_hours(168),
        'sum30d': sum_hours(720),
        'avg': sum(rates) / len(rates) if rates else 0,
        'max': max(rates) if rates else 0,
        'min': min(rates) if rates else 0,
        'count': len(data)
    }


def format_percent(value: float, decimals: int = 4) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.{decimals}f}%"


def generate_html(all_data: dict, main_count: int, hip3_count: int) -> str:
    """生成可搜索、可排序的 HTML 页面"""
    
    # 准备图表数据
    chart_data = {}
    for coin, data in all_data.items():
        if data['history']:
            chart_data[coin] = [
                {'time': d['time'], 'rate': float(d['fundingRate']) * 100}
                for d in sorted(data['history'], key=lambda x: x['time'])[-500:]
            ]
    
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hyperliquid 费率追踪</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #09090b; 
            color: #fafafa; 
            min-height: 100vh;
            padding: 24px;
        }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        
        .header {{ 
            display: flex; 
            align-items: center; 
            gap: 16px; 
            margin-bottom: 24px;
            flex-wrap: wrap;
        }}
        .header-icon {{
            width: 48px; height: 48px;
            background: linear-gradient(135deg, #10b981, #14b8a6);
            border-radius: 12px;
            display: flex; align-items: center; justify-content: center;
            flex-shrink: 0;
        }}
        .header-text h1 {{ font-size: 24px; font-weight: 700; }}
        .header-text p {{ color: #71717a; font-size: 14px; }}
        .stats-bar {{
            display: flex;
            gap: 16px;
            margin-left: auto;
            flex-wrap: wrap;
        }}
        .stats-item {{
            background: #18181b;
            border: 1px solid #27272a;
            border-radius: 8px;
            padding: 8px 16px;
            text-align: center;
        }}
        .stats-item .label {{ font-size: 11px; color: #71717a; text-transform: uppercase; }}
        .stats-item .value {{ font-size: 18px; font-weight: 600; color: #10b981; }}
        
        .controls {{
            display: flex;
            gap: 12px;
            margin-bottom: 16px;
            flex-wrap: wrap;
            align-items: center;
        }}
        .search-box {{
            flex: 1;
            min-width: 200px;
            max-width: 400px;
            position: relative;
        }}
        .search-box input {{
            width: 100%;
            padding: 10px 16px 10px 40px;
            background: #18181b;
            border: 1px solid #27272a;
            border-radius: 8px;
            color: #fafafa;
            font-size: 14px;
            outline: none;
            transition: border-color 0.2s;
        }}
        .search-box input:focus {{ border-color: #10b981; }}
        .search-box input::placeholder {{ color: #52525b; }}
        .search-box svg {{
            position: absolute;
            left: 12px;
            top: 50%;
            transform: translateY(-50%);
            color: #52525b;
        }}
        
        .filter-btns {{
            display: flex;
            gap: 8px;
        }}
        .filter-btn {{
            padding: 8px 16px;
            background: #18181b;
            border: 1px solid #27272a;
            border-radius: 6px;
            color: #a1a1aa;
            font-size: 13px;
            cursor: pointer;
            transition: all 0.2s;
        }}
        .filter-btn:hover {{ border-color: #3f3f46; color: #fafafa; }}
        .filter-btn.active {{ background: #10b981; border-color: #10b981; color: #000; }}
        
        .table-container {{
            background: rgba(24, 24, 27, 0.5);
            border: 1px solid #27272a;
            border-radius: 12px;
            overflow: hidden;
        }}
        .table-scroll {{
            max-height: 600px;
            overflow-y: auto;
        }}
        table {{ 
            width: 100%; 
            border-collapse: collapse;
        }}
        thead {{
            position: sticky;
            top: 0;
            z-index: 10;
        }}
        th {{ 
            background: #1f1f23;
            padding: 12px 12px;
            text-align: left;
            font-size: 11px;
            font-weight: 600;
            color: #a1a1aa;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            cursor: pointer;
            user-select: none;
            white-space: nowrap;
            border-bottom: 1px solid #27272a;
        }}
        th:hover {{ background: #27272a; }}
        th:not(:first-child) {{ text-align: right; }}
        th .sort-icon {{ margin-left: 4px; opacity: 0.5; }}
        th.sorted .sort-icon {{ opacity: 1; color: #10b981; }}
        
        td {{ 
            padding: 10px 12px;
            border-top: 1px solid rgba(39, 39, 42, 0.5);
            font-size: 13px;
        }}
        td:not(:first-child) {{ 
            text-align: right; 
            font-family: 'SF Mono', Monaco, 'Courier New', monospace;
            font-size: 12px;
        }}
        tbody tr {{ 
            cursor: pointer; 
            transition: background 0.15s; 
        }}
        tbody tr:hover {{ background: rgba(39, 39, 42, 0.3); }}
        tbody tr.selected {{ 
            background: rgba(16, 185, 129, 0.15); 
            border-left: 3px solid #10b981;
        }}
        tbody tr.hip3 .coin-name {{ color: #fbbf24; }}
        
        .coin-cell {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .coin-name {{ font-weight: 600; }}
        .coin-tag {{
            font-size: 10px;
            padding: 2px 6px;
            border-radius: 4px;
            background: #27272a;
            color: #71717a;
        }}
        .coin-tag.hip3 {{ background: rgba(251, 191, 36, 0.2); color: #fbbf24; }}
        
        .positive {{ color: #10b981; }}
        .negative {{ color: #f43f5e; }}
        .neutral {{ color: #71717a; }}
        
        .detail-panel {{
            position: fixed;
            top: 0;
            right: 0;
            width: 500px;
            height: 100vh;
            background: #18181b;
            border-left: 1px solid #27272a;
            padding: 24px;
            transform: translateX(100%);
            transition: transform 0.3s ease;
            overflow-y: auto;
            z-index: 100;
        }}
        .detail-panel.active {{ transform: translateX(0); }}
        .detail-overlay {{
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.5);
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.3s;
            z-index: 99;
        }}
        .detail-overlay.active {{ opacity: 1; pointer-events: auto; }}
        
        .detail-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 24px;
        }}
        .detail-title {{ font-size: 24px; font-weight: 700; }}
        .detail-subtitle {{ color: #71717a; font-size: 14px; margin-top: 4px; }}
        .close-btn {{
            width: 36px; height: 36px;
            background: #27272a;
            border: none;
            border-radius: 50%;
            color: #a1a1aa;
            cursor: pointer;
            font-size: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .close-btn:hover {{ background: #3f3f46; color: #fff; }}
        
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 12px;
            margin-bottom: 24px;
        }}
        .stat-card {{
            background: #09090b;
            border: 1px solid #27272a;
            border-radius: 8px;
            padding: 12px;
        }}
        .stat-label {{ 
            font-size: 11px; 
            color: #71717a; 
            text-transform: uppercase;
            margin-bottom: 4px;
        }}
        .stat-value {{ 
            font-size: 18px; 
            font-weight: 600; 
            font-family: 'SF Mono', Monaco, monospace;
        }}
        
        .chart-container {{
            background: #09090b;
            border: 1px solid #27272a;
            border-radius: 12px;
            padding: 16px;
            height: 250px;
        }}
        
        .no-results {{
            text-align: center;
            padding: 48px;
            color: #52525b;
        }}
        
        .update-time {{
            text-align: center;
            margin-top: 16px;
            color: #52525b;
            font-size: 12px;
        }}
        
        @media (max-width: 768px) {{
            .detail-panel {{ width: 100%; }}
            .stats-bar {{ width: 100%; justify-content: center; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-icon">
                <svg width="24" height="24" fill="none" stroke="white" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"/>
                </svg>
            </div>
            <div class="header-text">
                <h1>Hyperliquid 费率追踪</h1>
                <p>全交易对资金费率监控</p>
            </div>
            <div class="stats-bar">
                <div class="stats-item">
                    <div class="label">主 Perp</div>
                    <div class="value">{main_count}</div>
                </div>
                <div class="stats-item">
                    <div class="label">TradFi</div>
                    <div class="value" style="color: #fbbf24;">{hip3_count}</div>
                </div>
                <div class="stats-item">
                    <div class="label">总计</div>
                    <div class="value">{main_count + hip3_count}</div>
                </div>
            </div>
        </div>
        
        <div class="controls">
            <div class="search-box">
                <svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>
                </svg>
                <input type="text" id="search" placeholder="搜索交易对... (按 / 聚焦)" oninput="filterTable()">
            </div>
            <div class="filter-btns">
                <button class="filter-btn active" onclick="setFilter('all')">全部</button>
                <button class="filter-btn" onclick="setFilter('main')">主 Perp</button>
                <button class="filter-btn" onclick="setFilter('hip3')">TradFi</button>
                <button class="filter-btn" onclick="setFilter('positive')">正费率</button>
                <button class="filter-btn" onclick="setFilter('negative')">负费率</button>
            </div>
        </div>
        
        <div class="table-container">
            <div class="table-scroll">
                <table>
                    <thead>
                        <tr>
                            <th onclick="sortTable('name')" data-col="name">
                                交易对 <span class="sort-icon">↕</span>
                            </th>
                            <th onclick="sortTable('volume24h')" data-col="volume24h">
                                24H成交额 <span class="sort-icon">↕</span>
                            </th>
                            <th onclick="sortTable('openInterest')" data-col="openInterest">
                                持仓量 <span class="sort-icon">↕</span>
                            </th>
                            <th onclick="sortTable('rate8h')" data-col="rate8h">
                                8H费率 <span class="sort-icon">↕</span>
                            </th>
                            <th onclick="sortTable('sum1d')" data-col="sum1d">
                                1天 <span class="sort-icon">↕</span>
                            </th>
                            <th onclick="sortTable('sum7d')" data-col="sum7d">
                                7天 <span class="sort-icon">↕</span>
                            </th>
                            <th onclick="sortTable('sum30d')" data-col="sum30d">
                                30天 <span class="sort-icon">↕</span>
                            </th>
                        </tr>
                    </thead>
                    <tbody id="coin-table"></tbody>
                </table>
            </div>
            <div class="no-results" id="no-results" style="display: none;">
                没有找到匹配的交易对
            </div>
        </div>
        
        <div class="update-time">
            更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 点击任意行查看详情 | 按 / 搜索 | 按 ESC 关闭详情
        </div>
    </div>
    
    <div class="detail-overlay" id="detail-overlay" onclick="hideDetail()"></div>
    <div class="detail-panel" id="detail-panel">
        <div class="detail-header">
            <div>
                <div class="detail-title" id="detail-title">-</div>
                <div class="detail-subtitle">历史资金费率分析</div>
            </div>
            <button class="close-btn" onclick="hideDetail()">×</button>
        </div>
        <div class="stats-grid" id="detail-stats"></div>
        <div class="chart-container">
            <canvas id="funding-chart"></canvas>
        </div>
    </div>
    
    <script>
        const allData = {json.dumps({k: {'stats': v['stats'], 'market': v.get('market', {})} for k, v in all_data.items()}, ensure_ascii=False)};
        const chartData = {json.dumps(chart_data, ensure_ascii=False)};
        
        let currentSort = {{ col: 'volume24h', dir: 'desc' }};
        let currentFilter = 'all';
        let chart = null;
        
        const tableData = Object.entries(allData)
            .filter(([coin, data]) => data.stats)
            .map(([coin, data]) => ({{
                name: coin,
                isHip3: coin.startsWith('xyz:'),
                displayName: coin.replace('xyz:', ''),
                volume24h: data.market?.volume24h || 0,
                openInterest: data.market?.openInterest || 0,
                ...data.stats
            }}));
        
        function formatPercent(value, decimals = 4) {{
            if (value === null || value === undefined) return '-';
            return (value * 100).toFixed(decimals) + '%';
        }}
        
        function formatMoney(value) {{
            if (value === null || value === undefined || value === 0) return '-';
            if (value >= 1e9) return '$' + (value / 1e9).toFixed(2) + 'B';
            if (value >= 1e6) return '$' + (value / 1e6).toFixed(2) + 'M';
            if (value >= 1e3) return '$' + (value / 1e3).toFixed(1) + 'K';
            return '$' + value.toFixed(0);
        }}
        
        function formatOI(value) {{
            if (value === null || value === undefined || value === 0) return '-';
            if (value >= 1e6) return (value / 1e6).toFixed(2) + 'M';
            if (value >= 1e3) return (value / 1e3).toFixed(1) + 'K';
            return value.toFixed(0);
        }}
        
        function formatWithAnnual(value, days, decimals = 2) {{
            if (value === null || value === undefined) return '-';
            const pct = (value * 100).toFixed(decimals) + '%';
            const annual = (value * 365 / days * 100).toFixed(1) + '%';
            return `${{pct}} <span style="color:#71717a;font-size:10px">(${{annual}})</span>`;
        }}
        
        function getColorClass(val) {{
            if (val > 0.00001) return 'positive';
            if (val < -0.00001) return 'negative';
            return 'neutral';
        }}
        
        function renderTable() {{
            const tbody = document.getElementById('coin-table');
            const search = document.getElementById('search').value.toLowerCase();
            
            let filtered = tableData.filter(row => {{
                if (search && !row.name.toLowerCase().includes(search)) return false;
                if (currentFilter === 'main' && row.isHip3) return false;
                if (currentFilter === 'hip3' && !row.isHip3) return false;
                if (currentFilter === 'positive' && row.rate8h <= 0) return false;
                if (currentFilter === 'negative' && row.rate8h >= 0) return false;
                return true;
            }});
            
            filtered.sort((a, b) => {{
                let aVal = a[currentSort.col];
                let bVal = b[currentSort.col];
                
                if (currentSort.col === 'name') {{
                    aVal = a.displayName.toLowerCase();
                    bVal = b.displayName.toLowerCase();
                    return currentSort.dir === 'asc' ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
                }}
                return currentSort.dir === 'asc' ? aVal - bVal : bVal - aVal;
            }});
            
            document.querySelectorAll('th').forEach(th => {{
                th.classList.remove('sorted');
                const icon = th.querySelector('.sort-icon');
                if (icon) icon.textContent = '↕';
            }});
            const sortedTh = document.querySelector(`th[data-col="${{currentSort.col}}"]`);
            if (sortedTh) {{
                sortedTh.classList.add('sorted');
                sortedTh.querySelector('.sort-icon').textContent = currentSort.dir === 'asc' ? '↑' : '↓';
            }}
            
            if (filtered.length === 0) {{
                tbody.innerHTML = '';
                document.getElementById('no-results').style.display = 'block';
                return;
            }}
            
            document.getElementById('no-results').style.display = 'none';
            
            tbody.innerHTML = filtered.map(row => `
                <tr class="${{row.isHip3 ? 'hip3' : ''}}" onclick="showDetail('${{row.name}}')">
                    <td>
                        <div class="coin-cell">
                            <span class="coin-name">${{row.displayName}}</span>
                        </div>
                    </td>
                    <td style="color:#60a5fa">${{formatMoney(row.volume24h)}}</td>
                    <td style="color:#a78bfa">${{formatOI(row.openInterest)}}</td>
                    <td class="${{getColorClass(row.rate8h)}}">${{formatPercent(row.rate8h)}}</td>
                    <td class="${{getColorClass(row.sum1d)}}">${{formatWithAnnual(row.sum1d, 1, 3)}}</td>
                    <td class="${{getColorClass(row.sum7d)}}">${{formatWithAnnual(row.sum7d, 7, 2)}}</td>
                    <td class="${{getColorClass(row.sum30d)}}">${{formatWithAnnual(row.sum30d, 30, 2)}}</td>
                </tr>
            `).join('');
        }}
        
        function sortTable(col) {{
            if (currentSort.col === col) {{
                currentSort.dir = currentSort.dir === 'asc' ? 'desc' : 'asc';
            }} else {{
                currentSort.col = col;
                currentSort.dir = col === 'name' ? 'asc' : 'desc';
            }}
            renderTable();
        }}
        
        function setFilter(filter) {{
            currentFilter = filter;
            document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
            event.target.classList.add('active');
            renderTable();
        }}
        
        function filterTable() {{ renderTable(); }}
        
        function showDetail(coin) {{
            const data = allData[coin];
            if (!data || !data.stats) return;
            
            const stats = data.stats;
            const displayName = coin.replace('xyz:', '');
            
            document.getElementById('detail-title').textContent = displayName;
            
            document.getElementById('detail-stats').innerHTML = `
                <div class="stat-card">
                    <div class="stat-label">当前费率</div>
                    <div class="stat-value ${{getColorClass(stats.rate8h)}}">${{formatPercent(stats.rate8h)}}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">平均费率</div>
                    <div class="stat-value ${{getColorClass(stats.avg)}}">${{formatPercent(stats.avg)}}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">最高</div>
                    <div class="stat-value positive">${{formatPercent(stats.max)}}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">最低</div>
                    <div class="stat-value negative">${{formatPercent(stats.min)}}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">7天累计</div>
                    <div class="stat-value ${{getColorClass(stats.sum7d)}}">${{formatPercent(stats.sum7d, 2)}}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">30天累计</div>
                    <div class="stat-value ${{getColorClass(stats.sum30d)}}">${{formatPercent(stats.sum30d, 2)}}</div>
                </div>
            `;
            
            if (chart) chart.destroy();
            
            const ctx = document.getElementById('funding-chart').getContext('2d');
            const coinChartData = chartData[coin] || [];
            
            chart = new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels: coinChartData.map(d => new Date(d.time)),
                    datasets: [{{
                        label: '费率 %',
                        data: coinChartData.map(d => d.rate),
                        borderColor: '#10b981',
                        backgroundColor: 'rgba(16, 185, 129, 0.1)',
                        fill: true,
                        tension: 0.2,
                        pointRadius: 0,
                        pointHoverRadius: 4
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{
                        legend: {{ display: false }},
                        tooltip: {{
                            backgroundColor: '#18181b',
                            borderColor: '#3f3f46',
                            borderWidth: 1,
                            callbacks: {{ label: ctx => `费率: ${{ctx.parsed.y.toFixed(4)}}%` }}
                        }}
                    }},
                    scales: {{
                        x: {{
                            type: 'time',
                            time: {{ unit: 'day' }},
                            grid: {{ color: '#27272a' }},
                            ticks: {{ color: '#71717a', maxTicksLimit: 6 }}
                        }},
                        y: {{
                            grid: {{ color: '#27272a' }},
                            ticks: {{ color: '#71717a', callback: v => v.toFixed(3) + '%' }}
                        }}
                    }}
                }}
            }});
            
            document.getElementById('detail-panel').classList.add('active');
            document.getElementById('detail-overlay').classList.add('active');
            document.querySelectorAll('tbody tr').forEach(tr => tr.classList.remove('selected'));
            event.currentTarget.classList.add('selected');
        }}
        
        function hideDetail() {{
            document.getElementById('detail-panel').classList.remove('active');
            document.getElementById('detail-overlay').classList.remove('active');
            document.querySelectorAll('tbody tr').forEach(tr => tr.classList.remove('selected'));
        }}
        
        document.addEventListener('keydown', e => {{
            if (e.key === 'Escape') hideDetail();
            if (e.key === '/' && e.target.tagName !== 'INPUT') {{
                e.preventDefault();
                document.getElementById('search').focus();
            }}
        }});
        
        renderTable();
    </script>
</body>
</html>
'''
    
    return html


def main():
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='Hyperliquid 全交易对费率追踪器')
    parser.add_argument('--main-perp', action='store_true', 
                        help='包含主 Perp 区资产（默认只获取 HIP-3 TradeFi）')
    args = parser.parse_args()
    
    print("🚀 Hyperliquid 全交易对费率追踪器")
    print("=" * 60)
    
    main_assets, hip3_assets, market_data = get_all_perp_assets(include_main_perp=args.main_perp)
    
    all_assets = main_assets + hip3_assets
    total_count = len(all_assets)
    
    print(f"\n📊 总计 {total_count} 个交易对")
    
    end_time = int(datetime.now().timestamp() * 1000)
    start_time = int((datetime.now() - timedelta(days=30)).timestamp() * 1000)
    
    print(f"📅 数据范围: 最近 30 天")
    
    all_data = {}
    success_count = 0
    
    # 串行获取所有资产数据（避免限流）
    print(f"\n⏰ 获取费率数据 (串行, 避免限流)...\n")
    
    for i, coin in enumerate(all_assets, 1):
        display_name = coin.replace('xyz:', '') if coin.startswith('xyz:') else coin
        is_hip3 = coin.startswith('xyz:')
        
        try:
            coin_key, data = fetch_coin_data(coin, start_time, end_time)
            
            # 合并 Volume/OI 数据
            if coin in market_data:
                data['market'] = market_data[coin]
            else:
                data['market'] = {'volume24h': 0, 'openInterest': 0, 'markPx': 0, 'funding': 0}
            
            all_data[coin_key] = data
            
            if data['stats']:
                success_count += 1
                status = f"✅ {data['stats']['count']} 条"
            else:
                status = "❌ 无数据"
                
        except Exception as e:
            all_data[coin] = {'history': [], 'stats': None, 'market': market_data.get(coin, {})}
            status = f"❌ {e}"
        
        tag = "[TradeFi] " if is_hip3 else ""
        print(f"[{i}/{total_count}] {tag}{display_name:<12} {status}")
        
        # 请求间隔 0.3 秒，避免触发限流
        time.sleep(0.3)
    
    print(f"\n✅ 成功获取 {success_count}/{total_count} 个交易对")
    
    print("\n📝 生成报告...")
    html = generate_html(all_data, len(main_assets), len(hip3_assets))
    
    output_file = "hype_funding_report.html"
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)
    
    print(f"✅ 报告已保存: {os.path.abspath(output_file)}")
    
    print("🌐 正在打开浏览器...")
    webbrowser.open(f"file://{os.path.abspath(output_file)}")
    
    print("\n" + "=" * 60)
    print("📊 费率 TOP 10 (7天累计绝对值):")
    print("-" * 60)
    
    sorted_coins = sorted(
        [(k, v) for k, v in all_data.items() if v['stats']],
        key=lambda x: abs(x[1]['stats']['sum7d']),
        reverse=True
    )[:10]
    
    for coin, data in sorted_coins:
        s = data['stats']
        name = coin.replace('xyz:', '')
        print(f"{name:<12} 7天: {format_percent(s['sum7d'], 2):>10}")
    
    print("=" * 60)


if __name__ == "__main__":
    main()

