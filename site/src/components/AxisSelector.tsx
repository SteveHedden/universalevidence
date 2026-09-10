import { useEffect, useId, useMemo, useState } from "react";

import { searchTaxonomy } from "../api/client";
import type { TaxonomyClass, TaxonomyTerm } from "../api/client";

type AxisSelectorProps = {
  axis: TaxonomyClass;
  label: string;
  placeholder: string;
  value?: TaxonomyTerm | null;
  onChange: (term: TaxonomyTerm | null) => void;
  onInputChange?: (value: string) => void;
};

const DEBOUNCE_MS = 250;

export function AxisSelector({
  axis,
  label,
  placeholder,
  value,
  onChange,
  onInputChange,
}: AxisSelectorProps) {
  const inputId = useId();
  const listboxId = `${inputId}-listbox`;
  const [inputValue, setInputValue] = useState("");
  const [selected, setSelected] = useState<TaxonomyTerm | null>(null);
  const [options, setOptions] = useState<TaxonomyTerm[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasSearched, setHasSearched] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (value?.uri !== selected?.uri) {
      setSelected(value ?? null);
      setInputValue(value?.label ?? "");
    }
  }, [selected?.uri, value]);

  useEffect(() => {
    const query = inputValue.trim();
    if (query.length < 2 || selected?.label === inputValue) {
      setOptions([]);
      setLoading(false);
      setError(null);
      setHasSearched(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    setOpen(true);

    const timer = window.setTimeout(() => {
      searchTaxonomy(axis, query)
        .then((terms) => {
          if (cancelled) {
            return;
          }
          setOptions(terms);
          setHasSearched(true);
        })
        .catch(() => {
          if (cancelled) {
            return;
          }
          setOptions([]);
          setError("Unable to load terms");
          setHasSearched(true);
        })
        .finally(() => {
          if (!cancelled) {
            setLoading(false);
          }
        });
    }, DEBOUNCE_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [axis, inputValue, selected?.label]);

  const status = useMemo(() => {
    if (loading) {
      return "Loading terms";
    }
    if (error) {
      return error;
    }
    if (hasSearched && options.length === 0) {
      return axis === "region" ? "No regions available yet" : "No matching terms";
    }
    return null;
  }, [axis, error, hasSearched, loading, options.length]);

  function selectOption(term: TaxonomyTerm) {
    setSelected(term);
    setInputValue(term.label);
    setOptions([]);
    setOpen(false);
    setHasSearched(false);
    onChange(term);
    onInputChange?.(term.label);
  }

  function handleInputChange(value: string) {
    setInputValue(value);
    onInputChange?.(value);
    if (selected && value !== selected.label) {
      setSelected(null);
      onChange(null);
    }
  }

  return (
    <div className="axis-selector">
      <label htmlFor={inputId}>{label}</label>
      <div className="combobox">
        <input
          id={inputId}
          role="combobox"
          aria-autocomplete="list"
          aria-controls={listboxId}
          aria-expanded={open}
          autoComplete="off"
          value={inputValue}
          placeholder={placeholder}
          onChange={(event) => handleInputChange(event.target.value)}
          onFocus={() => setOpen(true)}
        />
        {selected ? (
          <button
            type="button"
            className="clear-selection"
            aria-label={`Clear ${label}`}
            onClick={() => {
              setSelected(null);
              setInputValue("");
              onChange(null);
              onInputChange?.("");
            }}
          >
            ×
          </button>
        ) : null}
      </div>
      {open && (status || options.length > 0) ? (
        <div className="option-popover">
          {status ? <div className="option-status">{status}</div> : null}
          {options.length > 0 ? (
            <ul id={listboxId} role="listbox" aria-label={`${label} options`}>
              {options.map((term) => (
                <li key={term.uri} role="option" aria-selected={selected?.uri === term.uri}>
                  <button type="button" onClick={() => selectOption(term)}>
                    <strong>{term.label}</strong>
                    {term.definition ? <span>{term.definition}</span> : null}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
