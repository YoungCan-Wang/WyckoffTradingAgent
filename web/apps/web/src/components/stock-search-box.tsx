import {
  useDeferredValue,
  useEffect,
  useState,
  type Dispatch,
  type FocusEvent,
  type KeyboardEvent,
  type SetStateAction,
} from 'react'
import { Loader2, Search } from 'lucide-react'
import { usePreferences } from '@/lib/preferences'
import { marketLabel, searchStocks, type StockSearchResult } from '@/lib/market-search'

export interface StockSearchController {
  symbol: string
  selectedStock: StockSearchResult | null
  suggestions: StockSearchResult[]
  searchOpen: boolean
  searching: boolean
  activeIndex: number
  setSymbol: Dispatch<SetStateAction<string>>
  setSelectedStock: Dispatch<SetStateAction<StockSearchResult | null>>
  setSearchOpen: Dispatch<SetStateAction<boolean>>
  setActiveIndex: Dispatch<SetStateAction<number>>
  updateSymbol: (value: string) => void
  selectSuggestion: (item: StockSearchResult) => void
}

export type StockSearchMode = 'analysis' | 'export'

export function displayStockCode(item: StockSearchResult, mode: StockSearchMode = 'analysis'): string {
  if (mode === 'export') return item.symbol || item.analysisCode
  return item.analysisCode
}

export function useStockSearch(mode: StockSearchMode = 'analysis'): StockSearchController {
  const [symbol, setSymbol] = useState('')
  const deferredSymbol = useDeferredValue(symbol)
  const [selectedStock, setSelectedStock] = useState<StockSearchResult | null>(null)
  const [searchOpen, setSearchOpen] = useState(false)
  const suggestionState = useSuggestionSearch(deferredSymbol, selectedStock, mode)

  function updateSymbol(value: string) {
    setSymbol(value)
    setSelectedStock(null)
    setSearchOpen(true)
  }

  function selectSuggestion(item: StockSearchResult) {
    setSelectedStock(item)
    setSymbol(displayStockCode(item, mode))
    setSearchOpen(false)
  }

  return {
    symbol,
    selectedStock,
    suggestions: suggestionState.suggestions,
    searchOpen,
    searching: suggestionState.searching,
    activeIndex: suggestionState.activeIndex,
    setSymbol,
    setSelectedStock,
    setSearchOpen,
    setActiveIndex: suggestionState.setActiveIndex,
    updateSymbol,
    selectSuggestion,
  }
}

function useSuggestionSearch(
  queryValue: string,
  selectedStock: StockSearchResult | null,
  mode: StockSearchMode,
) {
  const [suggestions, setSuggestions] = useState<StockSearchResult[]>([])
  const [searching, setSearching] = useState(false)
  const [activeIndex, setActiveIndex] = useState(0)

  useEffect(() => {
    const query = queryValue.trim()
    if (!query || matchesSelectedQuery(query, selectedStock, mode)) {
      setSuggestions([])
      setSearching(false)
      return
    }
    let cancelled = false
    setSearching(true)
    searchStocks(query, 8)
      .then((rows) => {
        if (cancelled) return
        setSuggestions(rows)
        setActiveIndex(0)
      })
      .finally(() => {
        if (!cancelled) setSearching(false)
      })
    return () => {
      cancelled = true
    }
  }, [queryValue, selectedStock, mode])

  return { suggestions, searching, activeIndex, setActiveIndex }
}

export function matchesSelectedQuery(
  query: string,
  selected: StockSearchResult | null,
  mode: StockSearchMode = 'analysis',
): boolean {
  if (!selected) return false
  const q = query.trim().toUpperCase()
  return [displayStockCode(selected, mode), selected.analysisCode, selected.symbol]
    .map((value) => value.trim().toUpperCase())
    .includes(q)
}

export function StockSearchBox({
  search,
  onSubmit,
  onClearError,
  placeholder,
  listboxId = 'stock-search-results',
}: {
  search: StockSearchController
  onSubmit: () => void
  onClearError?: () => void
  placeholder: string
  listboxId?: string
}) {
  const { t } = usePreferences()

  function handleChange(value: string) {
    search.updateSymbol(value)
    onClearError?.()
  }

  return (
    <div className="relative min-w-[240px] flex-1" onBlur={(e) => closeSearchOnOuterBlur(e, search.setSearchOpen)}>
      <label className="mb-1.5 block text-xs font-semibold text-muted-foreground">{t('common.stockCode')}</label>
      <div className="relative">
        <Search size={15} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-muted-foreground/60" />
        <input
          type="text"
          value={search.symbol}
          onChange={(e) => handleChange(e.target.value)}
          onFocus={() => search.setSearchOpen(true)}
          placeholder={placeholder}
          maxLength={28}
          className="w-full rounded-xl border border-border bg-background/50 py-2.5 pl-10 pr-4 text-sm font-semibold outline-none transition-all duration-200 placeholder:text-muted-foreground/50 focus:border-primary focus:bg-background focus:ring-2 focus:ring-primary/20"
          onKeyDown={(e) => handleSearchKeyDown(e, search, onSubmit)}
          role="combobox"
          aria-expanded={search.searchOpen && search.suggestions.length > 0}
          aria-controls={listboxId}
        />
      </div>
      <SearchSuggestions search={search} listboxId={listboxId} />
    </div>
  )
}

function SearchSuggestions({ search, listboxId }: { search: StockSearchController; listboxId: string }) {
  const { t } = usePreferences()
  if (!search.searchOpen || !search.symbol.trim()) return null
  return (
    <div
      id={listboxId}
      className="absolute z-20 mt-2 max-h-72 w-full overflow-auto rounded-xl border border-border/80 bg-popover/95 py-1.5 shadow-xl backdrop-blur-md animate-fade-in-up"
      role="listbox"
    >
      {search.searching && <LoadingSuggestion text={t('analysis.searching')} />}
      {!search.searching && search.suggestions.length === 0 && (
        <div className="px-3 py-2 text-sm text-muted-foreground">{t('analysis.noSearchResults')}</div>
      )}
      {!search.searching &&
        search.suggestions.map((item, index) => (
          <SuggestionRow
            key={`${item.market}:${item.analysisCode}`}
            item={item}
            active={index === search.activeIndex}
            onClick={() => search.selectSuggestion(item)}
          />
        ))}
    </div>
  )
}

function SuggestionRow({
  item,
  active,
  onClick,
}: {
  item: StockSearchResult
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      role="option"
      aria-selected={active}
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      className={`flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm hover:bg-muted ${active ? 'bg-muted' : ''}`}
    >
      <span className="min-w-0">
        <span className="block truncate font-medium">{item.name || item.analysisCode}</span>
        <span className="block truncate text-xs text-muted-foreground">
          {item.analysisCode} · {marketLabel(item.market)}
          {item.assetType === 'etf' ? ' · ETF' : ''}
        </span>
      </span>
      <span className="shrink-0 rounded-full border border-border px-2 py-0.5 text-[11px] text-muted-foreground">
        {item.market.toUpperCase()}
      </span>
    </button>
  )
}

function LoadingSuggestion({ text }: { text: string }) {
  return (
    <div className="flex items-center gap-2 px-3 py-2 text-sm text-muted-foreground">
      <Loader2 size={14} className="animate-spin" />
      {text}
    </div>
  )
}

function closeSearchOnOuterBlur(
  e: FocusEvent<HTMLDivElement>,
  setSearchOpen: Dispatch<SetStateAction<boolean>>,
) {
  const next = e.relatedTarget as Node | null
  if (!next || !e.currentTarget.contains(next)) setSearchOpen(false)
}

function handleSearchKeyDown(
  e: KeyboardEvent<HTMLInputElement>,
  search: StockSearchController,
  onSubmit: () => void,
) {
  if (!search.searchOpen || search.suggestions.length === 0) {
    if (e.key === 'Enter') onSubmit()
    return
  }
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    search.setActiveIndex((idx) => Math.min(idx + 1, search.suggestions.length - 1))
    return
  }
  if (e.key === 'ArrowUp') {
    e.preventDefault()
    search.setActiveIndex((idx) => Math.max(idx - 1, 0))
    return
  }
  if (e.key !== 'Enter') return
  e.preventDefault()
  const item = search.suggestions[search.activeIndex]
  if (item) search.selectSuggestion(item)
  else onSubmit()
}
