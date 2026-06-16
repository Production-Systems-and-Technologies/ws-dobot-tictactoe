import random

def get_winner_and_cells(board):
    """
    Checks rows, columns, and diagonals.

    Returns
    -------
    winner : 'X', 'O', or None
    winning_cells : list of (row, col) or None
    """
    # Rows
    for r in range(3):
        if board[r][0] and board[r][0] == board[r][1] == board[r][2]:
            return board[r][0], [(r, 0), (r, 1), (r, 2)]

    # Columns
    for c in range(3):
        if board[0][c] and board[0][c] == board[1][c] == board[2][c]:
            return board[0][c], [(0, c), (1, c), (2, c)]

    # Diagonals
    if board[1][1] and board[0][0] == board[1][1] == board[2][2]:
        return board[0][0], [(0, 0), (1, 1), (2, 2)]
    if board[1][1] and board[0][2] == board[1][1] == board[2][0]:
        return board[0][2], [(0, 2), (1, 1), (2, 0)]

    return None, None


def is_draw(board):
    """Return True if the board is full."""
    for r in range(3):
        for c in range(3):
            if board[r][c] == "":
                return False
    return True


class AIPlayer:
    """Minimax-based Tic-Tac-Toe AI with three difficulty settings."""

    WIN_SCORE = 10
    LOSS_SCORE = -10

    def __init__(self, player='O', difficulty='easy'):
        """
        Parameters
        ----------
        player
            'X' or 'O' (the mark this AI controls).
        difficulty
            'easy' | 'medium' | 'hard'
        """
        self.player = player
        self.op = 'X' if player == 'O' else 'O'
        self.diff = difficulty
        self.memo = {}  # minimax memoisation cache

    # Difficulty Logic
    def get_move(self, board):
        """
        Return (row, col) for the next move, or None if no moves available.
        """
        if self.diff == 'hard':
            return self._best_move(board)

        if self.diff == 'medium' and random.random() < 0.6:
            return self._best_move(board)

        return self._random_move(board)

    # ―― helpers ―────────────────────────────────────────────────
    @staticmethod
    def _available(board):
        """Return list of available (row, col) moves."""
        return [(r, c) for r in range(3) for c in range(3) if not board[r][c]]

    # Select one move (row, col) at random from _available
    def _random_move(self, board):
        moves = self._available(board)
        return random.choice(moves) if moves else None

    # Try all available moves, run minimax, pick best scoring move
    def _best_move(self, board):
        best_score = float('-inf')
        best_moves = []

        for r, c in self._available(board):
            board[r][c] = self.player
            score = self._minimax(board, depth=0, maxim=False)
            board[r][c] = ""

            if score > best_score:
                best_score = score
                best_moves = [(r, c)]
            elif score == best_score:
                best_moves.append((r, c))

        return random.choice(best_moves) if best_moves else None

    # Minimax algorithm
    def _minimax(self, bd, depth, maxim):
        """
        Minimax search.

        maxim = True  -> AI's turn
        maxim = False -> opponent's turn
        """
        key = (*map(tuple, bd), maxim)
        if key in self.memo:
            return self.memo[key]

        win, _ = get_winner_and_cells(bd)
        if win == self.player:
            return self.WIN_SCORE - depth
        if win == self.op:
            return depth + self.LOSS_SCORE  # == depth - 10
        if is_draw(bd):
            return 0

        if maxim:
            best = float('-inf')
            mark = self.player
        else:
            best = float('inf')
            mark = self.op

        for r, c in self._available(bd):
            bd[r][c] = mark
            score = self._minimax(bd, depth + 1, not maxim)
            bd[r][c] = ""

            if maxim:
                best = max(best, score)
            else:
                best = min(best, score)

        self.memo[key] = best
        return best

# Memoization
# When minimax is called, it checks if the current board state and turn (maximizing or minimizing) have been computed before.
# If so, it retrieves the stored score from the memo dictionary, avoiding redundant calculations and speeding up the process.
