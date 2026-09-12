window.readResponse = async function(response) {
  const text = await response.text();
  let value;
  try { value = text ? JSON.parse(text) : {}; }
  catch { throw new Error(`服务暂时未能处理请求（HTTP ${response.status}），请稍后重试。`); }
  if (!response.ok) {
    throw new Error(typeof value.detail === 'string' ? value.detail : `请求失败（HTTP ${response.status}），请检查输入后重试。`);
  }
  return value;
};
