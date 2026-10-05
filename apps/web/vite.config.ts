import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';

const controlTarget = process.env.VITE_RING_CONTROL_URL || 'http://127.0.0.1:58101';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/health': {target: controlTarget},
      '/api': {target: controlTarget},
    },
  },
});
