// OrderBook.tsx — Re-export of BookPanel for Order Book studio naming parity.
import BookPanel, { BookChip, useBook, balanceRead } from "./BookPanel";
export type { BookView, Row, Ev } from "./BookPanel";

export { BookPanel, BookChip, useBook, balanceRead };
export default BookPanel;
