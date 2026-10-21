---
name: 课程成绩统计
description: 对传入的数字计算均值和中位数，并提供真实脚本证据。
---
# 课程成绩统计
先 read_skill，然后用 read_skill_resource 查看 scripts/summarize.py。
使用 run_skill_script 执行，args 是数字字符串数组，例如 ["12","18","24","30"]。
根据 stdout 回答，不编造执行结果。脚本还会生成 outputs/summary.json 文件。
