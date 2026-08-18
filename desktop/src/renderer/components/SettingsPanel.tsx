/** 设置面板的 React 侧入口。目前只接管「通用」页。 */
import { useSettings } from '../lib/useSettings'
import { GeneralPanel } from './GeneralPanel'

const t = (key: string) => window.WyckoffI18n.t(key)

export function SettingsPanel ({ section }: { section: string }) {
  const { data, loading, notes, save } = useSettings()

  if (loading) return <p className="empty">{t('common.loading')}</p>
  if (!data) return <p className="empty">{t('daemon.readSettingsFailed')}</p>
  if (section === 'general') return <GeneralPanel data={data} notes={notes} save={save} />
  return null
}
