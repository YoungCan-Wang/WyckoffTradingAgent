/** 设置面板的 React 侧入口。三页全部由 React 渲染。 */
import { useSettings } from '../lib/useSettings'
import { GeneralPanel } from './GeneralPanel'
import { AgentPanel } from './AgentPanel'
import { AccountPanel } from './AccountPanel'

const t = (key: string) => window.WyckoffI18n.t(key)

interface Props {
  section: string
  /** 外壳动作：系统消息、退出登录与配置刷新。 */
  onMessage: (text: string, isError?: boolean) => void
  onSignOut: () => void
  onConfigChanged: () => void
}

export function SettingsPanel ({ section, onMessage, onSignOut, onConfigChanged }: Props) {
  const { data, loading, notes, save, reload, flash } = useSettings()

  // 模型的角色变更、新增、删除都要让输入框上的选择器跟着变。
  const reloadAll = async () => {
    await reload()
    onConfigChanged()
  }

  // 账号页不依赖 settings_get，自己读 account，所以先分流出去 ——
  // 否则设置读失败时连账号页也打不开。
  if (section === 'account') return <AccountPanel onSignOut={onSignOut} />

  if (loading) return <p className="empty">{t('common.loading')}</p>
  if (!data) return <p className="empty">{t('daemon.readSettingsFailed')}</p>

  if (section === 'general') return <GeneralPanel data={data} notes={notes} save={save} />
  if (section === 'agent') {
    return (
      <AgentPanel
        data={data}
        notes={notes}
        save={save}
        reload={reloadAll}
        onMessage={onMessage}
        flash={flash}
      />
    )
  }
  return null
}
