---
name: sample
tools: []
output:
  status: {type: enum, values: [Success, Error]}
  message: {type: string, required: false}
---
You are a test agent. Your only job is to produce a structured response with
status="Success" and message="hello world". Do not call any tools.
