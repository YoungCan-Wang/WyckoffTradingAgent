/** 设置面板的 React 侧入口。三页全部由 React 渲染。 */
import { useSettings } from '../lib/useSettings'
import { GeneralPanel } from './GeneralPanel'
import { AgentPanel } from './AgentPanel'
import { AccountPanel } from './AccountPanel'
import { ArchivedChatsPanel } from './ArchivedChatsPanel'

const t = (key: string) => window.WyckoffI18n.t(key)

interface Props {
  section: string
  /** 外壳动作：系统消息、退出登录与配置刷新。 */
  onMessage: (text: string, isError?: boolean) => void
  onSignOut: () => void
  onConfigChanged: () => void
  /** 会话列表变了（恢复/删除归档会话）。和 onConfigChanged 是两回事 ——
      后者刷的是模型与外观，刷不到侧栏的会话列表。 */
  onSessionsChanged: () => void
}

export function SettingsPanel (
  { section, onMessage, onSignOut, onConfigChanged, onSessionsChanged }: Props
) {
  const { data, loading, notes, save, reload, flash } = useSettings()

  // 模型的角色变更、新增、删除都要让输入框上的选择器跟着变。
  const reloadAll = async () => {
    await reload()
    onConfigChanged()
  }

  // 账号页不依赖 settings_get，自己读 account，所以先分流出去 ——
  // 否则设置读失败时连账号页也打不开。
  if (section === 'account') return <AccountPanel onSignOut={onSignOut} />

  // 会话页同理：它读的是 chat_sessions，跟 settings_get 无关。
  // 放在 loading 判断之前，设置读不出来时这一页仍然能用。
  if (section === 'chats') {
    return <ArchivedChatsPanel onChanged={onSessionsChanged} onMessage={onMessage} />
  }

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
