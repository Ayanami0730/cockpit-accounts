import { useEffect } from 'react';
import { LocalClaudePage } from './pages/LocalClaudePage';
import './App.css';
import './LocalApp.css';

export default function ClaudePreviewApp() {
  useEffect(() => {
    document.title = 'Cockpit Tools Local · Claude';
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const sync = () => document.documentElement.setAttribute('data-theme', media.matches ? 'dark' : 'light');
    sync();
    media.addEventListener('change', sync);
    return () => media.removeEventListener('change', sync);
  }, []);
  return <div className="local-app">
    <header className="local-header">
      <div className="local-brand"><strong>Cockpit Tools Local</strong><span>Claude · 本机账号视图</span></div>
      <nav className="local-nav" aria-label="账号导航"><span className="btn btn-primary">Claude 账号</span></nav>
    </header>
    <main className="local-content"><LocalClaudePage readonly /></main>
  </div>;
}
