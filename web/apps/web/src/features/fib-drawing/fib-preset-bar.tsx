import { FIB_PRESETS, type FibPreset } from '@/lib/fib-drawing'
import { usePreferences, type TranslationKey } from '@/lib/preferences'

const PRESET_KEY: Record<FibPreset, TranslationKey> = {
  d120: 'fib.preset.d120',
  month: 'fib.preset.month',
  m6: 'fib.preset.m6',
  y1: 'fib.preset.y1',
  y3: 'fib.preset.y3',
}

export function FibPresetBar({ preset, onChange }: { preset: FibPreset; onChange: (preset: FibPreset) => void }) {
  const { t } = usePreferences()
  return (
    <div className="flex flex-wrap items-center gap-2" role="group" aria-label={t('fib.preset.label')}>
      <span className="text-xs font-semibold text-muted-foreground">{t('fib.preset.label')}</span>
      {FIB_PRESETS.map((id) => (
        <button
          key={id}
          type="button"
          aria-pressed={preset === id}
          onClick={() => onChange(id)}
          className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${preset === id ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted-foreground hover:bg-muted hover:text-foreground'}`}
        >
          {t(PRESET_KEY[id])}
        </button>
      ))}
    </div>
  )
}
