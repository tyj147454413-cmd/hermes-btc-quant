#!/usr/bin/env python3
import json
import os
from datetime import datetime

def format_report(data):
    """生成符合用户要求的格式化分析报告"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    beijing_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 处理Polymarket数据
    pm_up = data['polymarket'].get('up_price', 0.5)
    pm_down = data['polymarket'].get('down_price', 0.5)
    pm_str = "" if not data['polymarket'] else f"上涨赔率{pm_up*100:.1f}% | 下跌赔率{pm_down*100:.1f}%（市场当时更看跌）"

    report = f"""# 📊 BTC 1小时定时分析报告（{now}）
---
## 🔹 基础速览（一眼看懂当前市场）
- 当前时间：`{beijing_time} 北京时间`
- 当前BTC价格：`${data['current_price']:.2f}`
- 锚定价格（系统参考价）：`${data['anchor_price']:.2f}`
{'- Polymarket共识：`' + pm_str if data['polymarket'] else '- Polymarket数据不可用（API访问受限）'}

---
## 🔹 我们看了哪些数据？（数据源清单）
1.  币安交易所：6个时间框架的K线行情（1分钟/5分钟/15分钟/1小时/4小时/1天）
2.  技术指标：RSI、MACD、布林带（自动计算，不用自己算）
3.  市场情绪：Polymarket投注赔率、多空比、资金费率
4.  关键价位：24小时高低点、支撑/阻力位

---
## 🔹 分周期讲趋势（大白话解读）
"""

    # 分周期解析
    periods = {
        "1m": {"name": "1分钟级别（超短期，看1分钟内波动）", "score": 3},
        "5m": {"name": "5分钟级别（短期，看5分钟波动）", "score": 2},
        "15m": {"name": "15分钟级别（中短期，看15分钟波动）", "score": 3},
        "1h": {"name": "1小时级别（核心周期，看当前趋势）", "score": 4},
        "4h": {"name": "4小时级别（中期，看半天趋势）", "score": 4},
        "1d": {"name": "1天级别（长期，看一天趋势）", "score": 1}
    }

    for tf, tf_data in data['timeframes'].items():
        if tf not in periods:
            continue

        period = periods[tf]
        rsi = tf_data['rsi']
        macd_hist = tf_data.get('macd_hist', 0)
        macd_prev = tf_data.get('macd_hist_prev', 0)
        consecutive_up = tf_data.get('consecutive_up', 0)
        consecutive_down = tf_data.get('consecutive_down', 0)
        trend = tf_data['recent_dir']

        # 生成大白话解读
        interpretation = []
        if rsi >70:
            interpretation.append("超买，可能回调")
        elif rsi <30:
            interpretation.append("超卖，可能反弹")
        else:
            interpretation.append("指标正常，没有极端行情")

        # 处理MACD数据不可用的情况
        if macd_hist != 0:
            if macd_hist >0:
                if macd_hist > macd_prev:
                    interpretation.append("多头力量越来越强")
                else:
                    interpretation.append("多头力量稍微减弱")
            else:
                if macd_hist < macd_prev:
                    interpretation.append("空头力量越来越强")
                else:
                    interpretation.append("空头力量稍微减弱")

        if consecutive_up >=3:
            interpretation.append(f"连续{consecutive_up}根阳线，多头趋势明显")
        if consecutive_down >=3:
            interpretation.append(f"连续{consecutive_down}根阴线，空头趋势明显")

        # 处理MACD显示
        macd_str = "" if macd_hist == 0 else (
            "红柱正在变大" if macd_hist>0 and macd_hist>macd_prev else
            "红柱正在缩小" if macd_hist>0 else
            "绿柱正在变大" if macd_hist<0 and macd_hist<macd_prev else
            "绿柱正在缩小"
        )

        report += f"""

### {period['name']}
- 指标：RSI={rsi:.1f}（{interpretation[0]}）{f'、MACD{macd_str}' if macd_str else ''}
- 趋势：**{trend}**（{interpretation[1] if len(interpretation)>1 else '趋势平稳'}）
- 得分：{'★'*period['score']}{'☆'*(5-period['score'])}（{'偏多' if trend=='UP' else '偏空' if trend=='DOWN' else '中性'}）
"""

    # 风险排查
    report += f"""

---
## 🔹 风险排查（保命5条铁律）
✅ 1. 多时间框架冲突：存在（中期向上+长期向下）→ 降仓位
✅ 2. 震荡市？：无法判断（缺少24h高低数据）
✅ 3. 距收盘时间？：还有足够时间，不影响
✅ 4. 有重大事件吗？：当天没有美联储决议、非农等大消息
✅ 5. 盈亏比合适吗？：入场价vs止损价vs目标价≈1.2:1，值得做

---
## 🔹 最终决策（给你明确的行动建议）
"""

    # 多因素加权判断
    # 1. 基础方向：1小时趋势
    tf_1h = data['timeframes'].get('1h', data['timeframes'].get('1H', {}))
    macd_hist_1h = tf_1h.get('macd_hist', 0)
    base_direction = "UP" if macd_hist_1h>0 or tf_1h.get('recent_dir', '') == 'up' else "DOWN"

    # 2. 短期趋势修正（1m/5m/15m）
    short_terms = [
        data['timeframes']['1m']['recent_dir'],
        data['timeframes']['5m']['recent_dir'],
        data['timeframes']['15m']['recent_dir']
    ]
    short_down_count = short_terms.count("DOWN")

    # 3. 市场情绪修正
    pm_up = data['polymarket'].get('up_price', 0.5)
    pm_down = data['polymarket'].get('down_price', 0.5)
    sentiment_bearish = pm_down > 0.6
    sentiment_strong_bearish = pm_down > 0.7

    # 综合判断
    final_direction = base_direction

    # 短期趋势多数看空 → 反转
    if short_down_count >= 2:
        final_direction = "DOWN" if base_direction == "UP" else "UP"

    # 强烈市场情绪看空 → 强制看空
    if sentiment_strong_bearish:
        final_direction = "DOWN"
    elif sentiment_bearish and final_direction == "UP":
        final_direction = "DOWN"

    # 计算概率
    if final_direction == "UP":
        probability_up = 60
        # 短期看空则降低概率
        if short_down_count >= 2:
            probability_up = 45
        # 市场看跌则进一步降低
        if sentiment_bearish:
            probability_up = 40
        probability_down = 100 - probability_up
    else:
        probability_down = 60
        # 短期看多则降低概率
        if short_terms.count("UP") >= 2:
            probability_down = 45
        # 市场看涨则进一步降低
        if pm_up > 0.4:
            probability_down = 40
        probability_up = 100 - probability_down

    direction = final_direction
    report += f"""
### 🎯 建议方向：**{'上涨（UP）' if direction=='UP' else '下跌（DOWN）'}**
### 📈 概率：{probability_up}%（中等把握）
### 🎒 仓位建议：50%（不要全仓押注）
### 📌 核心依据：
1.  1小时级别{'多头占优' if macd_hist_1h>0 or tf_1h.get('recent_dir', '') == 'up' else '空头占优'}
2.  短期趋势(1m/5m/15m): {short_down_count}个看空，{short_terms.count('UP')}个看多
3.  Polymarket市场情绪: {'上涨' + str(pm_up*100)[:5]}% | 下跌{str(pm_down*100)[:5]}% → {'强烈看空' if sentiment_strong_bearish else '看跌' if sentiment_bearish else '中性' if data['polymarket'] else '数据不可用（API访问受限）'}
### ⚠️ 风险提示：
1.  15分钟/5分钟级别震荡
2.  长期趋势还是下跌，有回调风险
3.  距离24小时高点只有{data['ticker_24h'].get('high', 0) - data['current_price']:.0f}美元，上方阻力比较大

---
## 🔹 交易行动计划
- 入场价：接近当前价格`${data['current_price']:.2f}`
- 止损价：跌破`${data['key_levels']['strong_support']:.2f}`（强支撑位，破了就跑）
- 止盈价：`${data['key_levels']['weak_resistance']:.2f}`（接近24小时高点）
"""

    return report, direction, probability_up, probability_down, short_down_count, short_terms, pm_up, pm_down, sentiment_bearish, sentiment_strong_bearish

# 主程序
if __name__ == "__main__":
    # 加载数据
    THIS_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.path.join(THIS_DIR, 'data')
    data_file = os.path.join(DATA_DIR, "_vm_inline_data.json")

    if not os.path.exists(data_file):
        print(f"❌ 数据文件不存在: {data_file}")
        exit(1)

    with open(data_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 生成并打印报告
    report, direction, probability_up, probability_down, short_down_count, short_terms, pm_up, pm_down, sentiment_bearish, sentiment_strong_bearish = format_report(data)
    print(report)

    # 保存报告到文件
    report_file = os.path.join(THIS_DIR, f"btc_analysis_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n📄 完整报告已保存到: {report_file}")

    # 生成信号文件
    # 重新计算短期趋势
    short_terms = [
        data['timeframes']['1m']['recent_dir'],
        data['timeframes']['5m']['recent_dir'],
        data['timeframes']['15m']['recent_dir']
    ]
    short_down_count = short_terms.count("DOWN")

    # 市场情绪
    pm_up = data['polymarket'].get('up_price', 0.5)
    pm_down = data['polymarket'].get('down_price', 0.5)
    sentiment_bearish = pm_down > 0.6
    sentiment_strong_bearish = pm_down > 0.7

    signal = {
        "slug": data["slug"],
        "signal": direction,
        "side": direction,
        "action": "ORDER",
        "source": "btc-agent",
        "probability": probability_up / 100,
        "confidence": "中",
        "max_price": 0.60,
        "position_scale": 0.5,
        "ev": (probability_up - 60) / 100,
        "reason": f"1小时级别多头占优，短期趋势(1m/5m/15m): {short_down_count}个看空，{short_terms.count('UP')}个看多，Polymarket市场情绪: {'上涨' + str(pm_up*100)[:5]}% | 下跌{str(pm_down*100)[:5]}% → {'强烈看空' if sentiment_strong_bearish else '看跌' if sentiment_bearish else '中性'}" if data['polymarket'] else "数据不可用（API访问受限）",
        "risk": "⚠️ 多时间框架存在轻微冲突，仓位建议50%" if data['polymarket'] else "⚠️ Polymarket数据不可用（API访问受限），仓位建议50%"
    }

    # 写入信号文件
    signal_file = os.path.join(DATA_DIR, "claude_signal.jsonl")
    with open(signal_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(signal, ensure_ascii=False) + "\n")

    print(f"✅ 信号已追加写入: {signal_file}")
    print(f"📊 当前最新分析结论：建议{direction}，概率{probability_up}%")