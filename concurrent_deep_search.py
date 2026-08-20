#!/usr/bin/env python3
"""
Concurrent Deep Search Engine
Integrates Web Search + Multi-Session Headful Chrome (Nodriver Browser) + Qwen 3.6 MTP
"""

import argparse
import asyncio
import json
import os
import socket
import sys
import time
import urllib.request
import requests

SOCKET_PATH = os.path.expanduser("~/.pi/agent/nodriver-browser.sock")
QWEN_URL = "http://localhost:8001/v1/chat/completions"
MARKER = '__PI_NODRIVER__'

def get_tavily_api_key():
    key = os.environ.get("TAVILY_API_KEY")
    if not key and os.path.exists(os.path.expanduser("~/.hermes/.env")):
        with open(os.path.expanduser("~/.hermes/.env")) as f:
            for line in f:
                if line.startswith("TAVILY_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    return key

# --- Step 1: Web Search ---
def perform_web_search(query, max_results=5):
    t0 = time.perf_counter()
    api_key = get_tavily_api_key()
    if not api_key:
        raise ValueError("TAVILY_API_KEY is not set in environment or ~/.hermes/.env")
    
    resp = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": api_key, "query": query, "max_results": max_results * 2},
        timeout=15
    )
    t1 = time.perf_counter()
    
    if resp.status_code != 200:
        raise RuntimeError(f"Tavily search failed (code {resp.status_code}): {resp.text}")
    
    raw_results = resp.json().get("results", [])
    # Filter out pure video / unsupported domains if looking for textual articles
    filtered = []
    for r in raw_results:
        u = r.get("url", "")
        if "youtube.com" in u or "youtu.be" in u or "tiktok.com" in u:
            continue
        filtered.append(r)
        if len(filtered) >= max_results:
            break
            
    if not filtered and raw_results:
        filtered = raw_results[:max_results]
        
    return filtered, round(t1 - t0, 2)

# --- Step 2: Concurrent Browser Scraping ---
async def scrape_single_url(session_id, item, timeout_sec=25):
    url = item["url"]
    title = item.get("title", url)
    t0 = time.perf_counter()
    
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(SOCKET_PATH),
            timeout=10
        )
        
        async def send_cmd(cmd_str, req_id):
            req = {"id": req_id, "command": cmd_str, "sessionId": session_id}
            writer.write((json.dumps(req) + "\n").encode())
            await writer.drain()
            line = await reader.readline()
            raw = line.decode()
            if raw.startswith(MARKER):
                return json.loads(raw[len(MARKER):])
            return json.loads(raw)
        
        # 1. Open URL
        await send_cmd(f"open {url}", 1)
        # Render buffer
        await asyncio.sleep(1.2)
        
        # 2. Dismiss overlays
        try:
            await send_cmd("dismiss overlays --cookies=accept", 2)
        except Exception:
            pass
            
        # 3. Get text
        res = await send_cmd("get text", 3)
        raw_text = res.get("text", "")
        
        # 4. Close Tab
        try:
            await send_cmd("close", 4)
        except Exception:
            pass
            
        writer.close()
        await writer.wait_closed()
        
        t1 = time.perf_counter()
        
        # Basic clean
        lines = raw_text.splitlines()
        cleaned_lines = []
        for l in lines:
            s = l.strip()
            if not s:
                continue
            if any(s.lower().startswith(p) for p in ["cookie", "accept all", "jump to", "privacy policy", "terms of use"]):
                continue
            cleaned_lines.append(s)
            
        final_text = "\n".join(cleaned_lines)
        
        return {
            "success": True,
            "title": title,
            "url": url,
            "char_count": len(final_text),
            "text": final_text,
            "elapsed_sec": round(t1 - t0, 2)
        }
        
    except Exception as e:
        t1 = time.perf_counter()
        return {
            "success": False,
            "title": title,
            "url": url,
            "error": str(e),
            "text": item.get("content", ""), # Fallback to search snippet
            "char_count": len(item.get("content", "")),
            "elapsed_sec": round(t1 - t0, 2)
        }

async def concurrent_scrape_all(search_results):
    t0 = time.perf_counter()
    tasks = [
        scrape_single_url(f"deep_search_{i}", item)
        for i, item in enumerate(search_results)
    ]
    scraped_pages = await asyncio.gather(*tasks)
    t1 = time.perf_counter()
    return scraped_pages, round(t1 - t0, 2)

# --- Step 3: Synthesis with Qwen 3.6 MTP ---
def synthesize_with_qwen(query, scraped_pages):
    sources_text = []
    for i, page in enumerate(scraped_pages, 1):
        if page["success"] and page["text"]:
            content = page["text"][:3500]  # Take top 3500 chars per page
            sources_text.append(f"### [來源 {i}] {page['title']}\n網址: {page['url']}\n內文:\n{content}\n")
        elif page.get("text"):
            sources_text.append(f"### [來源 {i}] {page['title']} (摘要備援)\n網址: {page['url']}\n摘要:\n{page['text']}\n")

    all_context = "\n----------------------------------------\n".join(sources_text)
    
    prompt = (
        f"你是一位精通深度產業與情報分析的資深研究員。請根據以下多個網頁來源並發抓取的真實完整內文，"
        f"針對使用者的查詢「{query}」進行全面、嚴謹、結構化的深度繁體中文研究報告。\n\n"
        f"【報告要求】\n"
        f"1. 核心結論與 Executive Summary（重點摘要）\n"
        f"2. 詳細要點分析（包含具體數據、時間線、技術規格、產能或關鍵事實）\n"
        f"3. 跨來源比對與潛在風險/趨勢觀察\n"
        f"4. 標註資訊出處引用（如 [來源 1]）\n\n"
        f"--- 多來源完整網頁內文開始 ---\n{all_context}\n--- 多來源完整網頁內文結束 ---\n\n請輸出繁體中文分析報告："
    )
    
    payload = {
        "model": "qwen3.6-mtp",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
        "temperature": 0.3,
        "stream": False
    }
    
    t0 = time.perf_counter()
    req = urllib.request.Request(
        QWEN_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        res_data = json.loads(resp.read().decode("utf-8"))
    t1 = time.perf_counter()
    
    usage = res_data.get("usage", {})
    timings = res_data.get("timings", {})
    draft_n = timings.get("draft_n", 0)
    draft_acc = timings.get("draft_n_accepted", 0)
    draft_rate = (draft_acc / draft_n * 100.0) if draft_n > 0 else 0.0
    
    msg = res_data["choices"][0]["message"]
    report = msg.get("content") or msg.get("reasoning_content", "")
    
    return {
        "report": report,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "elapsed_sec": round(t1 - t0, 2),
        "predicted_tps": round(timings.get("predicted_per_second", 0), 1),
        "draft_acceptance_rate_pct": round(draft_rate, 1)
    }

# --- Main Pipeline Runner ---
def run_deep_search_pipeline(query, max_results=4):
    print(f"\n🔍 [1/3] 執行網路搜尋: 「{query}」...")
    results, search_sec = perform_web_search(query, max_results=max_results)
    print(f"✓ 搜尋完成 (耗時 {search_sec}s)，找到 {len(results)} 個目標網頁：")
    for idx, r in enumerate(results, 1):
        print(f"   {idx}. {r.get('title')[:45]} -> {r.get('url')[:60]}")
        
    print(f"\n⚡ [2/3] 啟動本機 Headful Chrome 多分頁並發爬取 (同時開啟 {len(results)} 個分頁)...")
    scraped, scrape_sec = asyncio.run(concurrent_scrape_all(results))
    total_chars = sum(p.get("char_count", 0) for p in scraped)
    success_count = sum(1 for p in scraped if p.get("success"))
    print(f"✓ 並發爬取完成 (總耗時 {scrape_sec}s)！")
    print(f"   - 成功率: {success_count}/{len(results)} | 總抓取文字量: {total_chars:,} 字")
    for idx, p in enumerate(scraped, 1):
        st = "✅" if p["success"] else "⚠️"
        print(f"   {st} [分頁 {idx}] {p['elapsed_sec']}s ({p['char_count']:,} 字) — {p['title'][:40]}")
        
    print(f"\n🧠 [3/3] 送入本機 Qwen 3.6 35B MTP 進行多來源跨文本交叉合成與深度推理...")
    synth_res = synthesize_with_qwen(query, scraped)
    print(f"✓ Qwen 3.6 生成完成！")
    print(f"   - 輸入 Tokens: {synth_res['prompt_tokens']} | 生成 Tokens: {synth_res['completion_tokens']}")
    print(f"   - 生成耗時: {synth_res['elapsed_sec']}s ({synth_res['predicted_tps']} TPS) | MTP 接受率: {synth_res['draft_acceptance_rate_pct']}%")
    
    total_pipeline_sec = round(search_sec + scrape_sec + synth_res['elapsed_sec'], 2)
    print(f"\n⏱️ === 端到端流水線總耗時: {total_pipeline_sec} 秒 ===")
    
    return {
        "query": query,
        "search_sec": search_sec,
        "scrape_sec": scrape_sec,
        "llm_sec": synth_res['elapsed_sec'],
        "total_sec": total_pipeline_sec,
        "sources": scraped,
        "llm_metrics": synth_res,
        "report": synth_res['report']
    }

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Concurrent Deep Search Pipeline")
    parser.add_argument("query", nargs="?", default="台積電 2nm 最新量產進度與蘋果A19晶片規劃", help="Search query")
    parser.add_argument("--limit", type=int, default=4, help="Number of URLs to concurrently scrape")
    args = parser.parse_args()
    
    out = run_deep_search_pipeline(args.query, max_results=args.limit)
    print("\n" + "="*70)
    print(out["report"])
    print("="*70)
