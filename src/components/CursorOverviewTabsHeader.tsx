import { Layers } from 'lucide-react';
import { CursorIcon } from './icons/CursorIcon';

export type CursorTab = 'overview' | 'instances';

interface CursorOverviewTabsHeaderProps {
  active: CursorTab;
  onTabChange?: (tab: CursorTab) => void;
}

export function CursorOverviewTabsHeader({
  active,
  onTabChange,
}: CursorOverviewTabsHeaderProps) {
  return (
    <div className="page-tabs-row page-tabs-center">
      <div className="page-tabs filter-tabs" role="tablist" aria-label="Cursor">
        <button className={`filter-tab${active === 'overview' ? ' active' : ''}`} role="tab" aria-selected={active === 'overview'} onClick={() => onTabChange?.('overview')}><CursorIcon className="tab-icon" /><span>账号总览</span></button>
        <button className={`filter-tab${active === 'instances' ? ' active' : ''}`} role="tab" aria-selected={active === 'instances'} onClick={() => onTabChange?.('instances')}><Layers className="tab-icon" /><span>应用多开</span></button>
      </div>
    </div>
  );
}
