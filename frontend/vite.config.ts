import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

/** 接收端口配置及默认值，返回合法 TCP 端口；非法配置明确报错，避免静默换端口。 */
function port(value: string | undefined, fallback: number): number {
  const result = value === undefined ? fallback : Number(value);
  if (!Number.isInteger(result) || result < 1 || result > 65535) throw new Error('前端或 API 端口必须为 1～65535 的整数');
  return result;
}

/** 接收构建模式，只读取开发与 API 端口；返回开发及本地预览配置，生产仍通过前端容器部署。 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '..', ['DEV_PORT', 'API_HOST_PORT']);
  // 开发与预览都走同源代理，保留原 Host 以通过后端 Origin 校验，流式连接不设置代理超时。
  const proxy = { '/api': { target: `http://127.0.0.1:${port(env.API_HOST_PORT, 8000)}`, changeOrigin: false, timeout: 0, proxyTimeout: 0 } };
  return {
    plugins: [react()],
    server: { host: '127.0.0.1', port: port(env.DEV_PORT, 8080), strictPort: true, proxy },
    preview: { host: '127.0.0.1', port: 4173, strictPort: true, proxy },
  };
});
