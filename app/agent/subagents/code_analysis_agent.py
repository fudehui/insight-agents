"""
数据分析子智能体配置模块

将 app/prompt/prompts.yml 中的 code_analysis 配置与 run_code 沙箱执行工具
组装成 DeepAgents 可识别的字典式子智能体。主智能体后续会根据 description
决定是否把定量分析、统计计算与图表生成任务分派给它。
"""

from app.agent.prompts import sub_agents_content
from app.tools.sandbox_tools import run_code

# 字典式子智能体的核心字段来自 YAML，便于后续只改配置就能调整路由描述和行为约束；
# tools 只暴露 run_code：分析助手不做信息检索，也不负责最终文件生成
code_analysis_agent = {
    "name": sub_agents_content["code_analysis"]["name"],
    "description": sub_agents_content["code_analysis"]["description"],
    "system_prompt": sub_agents_content["code_analysis"]["system_prompt"],
    "tools": [run_code],
}
