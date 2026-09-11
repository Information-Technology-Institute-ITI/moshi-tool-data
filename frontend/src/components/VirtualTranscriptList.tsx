import {
  type ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

const ESTIMATED_ROW_HEIGHT = 88;
const OVERSCAN = 6;

function MeasuredRow({ id, top, children, onHeight }: {
  id: string;
  top: number;
  children: ReactNode;
  onHeight: (id: string, height: number) => void;
}) {
  const element = useRef<HTMLLIElement>(null);
  useEffect(() => {
    const node = element.current;
    if (!node) return;
    const measure = () => onHeight(id, Math.ceil(node.getBoundingClientRect().height));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [id, onHeight]);
  return (
    <li ref={element} className="virtual-transcript-row" style={{ top }}>
      {children}
    </li>
  );
}

export default function VirtualTranscriptList<T extends { id: string }>({
  items,
  empty,
  selectedId,
  focusId,
  onManualScroll,
  renderItem,
}: {
  items: T[];
  empty: ReactNode;
  selectedId?: string | null;
  focusId?: string | null;
  onManualScroll?: () => void;
  renderItem: (item: T, index: number) => ReactNode;
}) {
  const list = useRef<HTMLOListElement>(null);
  const heights = useRef(new Map<string, number>());
  const [measurementVersion, setMeasurementVersion] = useState(0);
  const [viewport, setViewport] = useState({ top: 0, height: 480 });
  const offsets = useMemo(() => {
    let cursor = 0;
    const values = items.map((item) => {
      const top = cursor;
      cursor += heights.current.get(item.id) || ESTIMATED_ROW_HEIGHT;
      return top;
    });
    return { values, total: cursor };
  }, [items, measurementVersion]);
  const found = offsets.values.findIndex((top, index) => (
    top + (heights.current.get(items[index].id) || ESTIMATED_ROW_HEIGHT) >= viewport.top
  ));
  const firstVisible = found < 0 ? Math.max(0, items.length - 1) : found;
  let lastVisible = items.length;
  for (let index = firstVisible; index < items.length; index += 1) {
    if (offsets.values[index] > viewport.top + viewport.height) {
      lastVisible = index;
      break;
    }
  }
  const start = Math.max(0, firstVisible - OVERSCAN);
  const end = Math.min(items.length, lastVisible + OVERSCAN);
  const measure = (id: string, height: number) => {
    if (height <= 0 || heights.current.get(id) === height) return;
    heights.current.set(id, height);
    setMeasurementVersion((value) => value + 1);
  };

  useEffect(() => {
    const node = list.current;
    const targetId = focusId === undefined ? selectedId : focusId;
    const index = targetId ? items.findIndex((item) => item.id === targetId) : -1;
    if (!node || index < 0) return;
    const top = offsets.values[index];
    const bottom = top + (heights.current.get(items[index].id) || ESTIMATED_ROW_HEIGHT);
    if (top < node.scrollTop) node.scrollTop = top;
    else if (bottom > node.scrollTop + node.clientHeight) {
      node.scrollTop = Math.max(0, bottom - node.clientHeight);
    }
  }, [focusId, items, offsets, selectedId]);

  if (!items.length) return <ol className="transcript-list">{empty}</ol>;
  return (
    <ol
      ref={list}
      className="transcript-list virtual-transcript-list"
      onWheel={onManualScroll}
      onTouchStart={onManualScroll}
      onScroll={(event) => setViewport({
        top: event.currentTarget.scrollTop,
        height: event.currentTarget.clientHeight,
      })}
    >
      <li className="virtual-transcript-spacer" style={{ height: offsets.total }} aria-hidden="true" />
      {items.slice(start, end).map((item, offset) => {
        const index = start + offset;
        return (
          <MeasuredRow key={item.id} id={item.id} top={offsets.values[index]} onHeight={measure}>
            {renderItem(item, index)}
          </MeasuredRow>
        );
      })}
    </ol>
  );
}
