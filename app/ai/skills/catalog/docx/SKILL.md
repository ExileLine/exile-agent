---
name: docx
description: "Use this skill when the user wants a Word-style business document or .docx deliverable, including reports, plans, meeting minutes, contracts, letters, resumes, templates, proposals, summaries, or formatted deliverables. Triggers include 'Word', 'word document', '.docx', 'document', 'report', 'memo', 'letter', 'template', and Chinese business phrases like '生成文档', '创建文档', '季度报告', '月度报告', '会议纪要', '方案文档', '合同文档', '简历文档'. Do not use for PDFs, spreadsheets, slides, or general coding tasks."
tags:
  - docx
  - word
  - document
route_keywords:
  - Word
  - word document
  - word文档
  - Word文档
  - docx
  - .docx
  - 文档
  - 生成文档
  - 创建文档
  - 输出文档
  - 导出文档
  - 报告
  - 报告文档
  - 方案
  - 方案文档
  - 会议纪要
  - 合同
  - 合同文档
  - 模板
  - 模板文档
  - 简历
  - 简历文档
  - 季度报告
  - 月度报告
  - 周报
---

# DOCX Skill

Create and validate simple Word `.docx` business documents using bundled Python scripts.

## Runtime Rules

- Users do not need to mention this skill, scripts, or internal paths.
- For normal business requests, create a `.docx` artifact with `scripts/create_docx.py`.
- After creating a `.docx`, validate it with `scripts/validate_docx.py`.
- Do not create ad-hoc JavaScript files. The runtime exposes Python skill scripts, not arbitrary shell or Node execution.
- Use concise business content when details are missing; do not ask follow-up questions unless required fields are impossible to infer.
- In the final answer, include the generated file path and validation result.

## Script Usage

Create a document:

```bash
python scripts/create_docx.py \
  --output quarterly_report.docx \
  --title "季度报告" \
  --subtitle "业务摘要、里程碑和下季度计划" \
  --section "业务摘要::本季度业务保持稳定推进。" \
  --section "里程碑::完成核心功能交付。" \
  --section "下季度计划::继续推进产品优化和质量提升。"
```

Validate a document:

```bash
python scripts/validate_docx.py quarterly_report.docx
```

## Output Guidance

For a request like “帮我生成一份季度报告，包含业务摘要、里程碑和下季度计划。”:

1. Use `scripts/create_docx.py`.
2. Use a safe relative output filename such as `quarterly_report.docx`.
3. Add sections for `业务摘要`, `里程碑`, and `下季度计划`.
4. Run `scripts/validate_docx.py quarterly_report.docx`.
5. Return a short answer with the absolute path from the script output.
