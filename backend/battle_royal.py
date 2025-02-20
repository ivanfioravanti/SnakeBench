import random
from collections import deque
from typing import List, Tuple, Dict, Set, Optional
import time
from datetime import datetime
import os
from dotenv import load_dotenv
from openai import OpenAI
import json
import uuid
import argparse
from llm_providers import create_llm_provider

load_dotenv()

# Directions
UP = "UP"
DOWN = "DOWN"
LEFT = "LEFT"
RIGHT = "RIGHT"
VALID_MOVES = {UP, DOWN, LEFT, RIGHT}


class Snake:
    """
    Represents a snake on the board.
    positions: deque of (x, y) from head at index 0 to tail at the end
    alive: whether this snake is still alive
    """
    def __init__(self, positions: List[Tuple[int, int]]):
        self.positions = deque(positions)
        self.alive = True
        self.death_reason = None   # e.g., 'wall', 'self', 'collision'
        self.death_round = None    # The round number when the snake died

    @property
    def head(self) -> Tuple[int, int]:
        return self.positions[0]


class GameState:
    """
    A snapshot of the game at a specific point in time:
      - round_number: which round we are in (0-based)
      - snake_positions: dict of snake_id -> list of (x, y)
      - alive: dict of snake_id -> bool
      - scores: dict of snake_id -> int
      - width, height: board dimensions
      - apples: list of (x, y) positions of all apples on the board
      - move_history: list of dicts (one per round), each mapping snake_id -> move
    """
    def __init__(self,
                 round_number: int,
                 snake_positions: Dict[str, List[Tuple[int, int]]],
                 alive: Dict[str, bool],
                 scores: Dict[str, int],
                 width: int,
                 height: int,
                 apples: List[Tuple[int, int]],
                 move_history: List[Dict[str, str]]):
        self.round_number = round_number
        self.snake_positions = snake_positions
        self.alive = alive
        self.scores = scores
        self.width = width
        self.height = height
        self.apples = apples
        self.move_history = move_history

    def print_board(self) -> str:
        """
        Returns a string representation of the board with:
        . = empty space
        A = apple
        T = snake tail
        1,2,3... = snake head (showing player number)
        Now with (0,0) at bottom left and x-axis labels at bottom
        """
        # Create empty board
        board = [['.' for _ in range(self.width)] for _ in range(self.height)]
        
        # Place apples
        for ax, ay in self.apples:
            board[ay][ax] = 'A'
            
        # Place snakes
        for i, (snake_id, positions) in enumerate(self.snake_positions.items(), start=1):
            if not self.alive[snake_id]:
                continue
            
            # Place snake body
            for pos_idx, (x, y) in enumerate(positions):
                if pos_idx == 0:  # Head
                    board[y][x] = str(i)  # Use snake number (1, 2, 3...) for head
                else:  # Body/tail
                    board[y][x] = 'T'
        
        # Build the string representation
        result = []
        # Print rows in reverse order (bottom to top)
        for y in range(self.height - 1, -1, -1):
            result.append(f"{y:2d} {' '.join(board[y])}")
        
        # Add x-axis labels at the bottom
        result.append("   " + " ".join(str(i) for i in range(self.width)))
        
        return "\n".join(result)

    def __repr__(self):
        return (
            f"<GameState round={self.round_number}, apples={self.apples}, "
            f"snakes={len(self.snake_positions)}, scores={self.scores}>"
        )

class Player:
    """
    Base class/interface for player logic.
    Each player is responsible for returning a move for its snake_id 
    given the current game state.
    """
    def __init__(self, snake_id: str):
        self.snake_id = snake_id

    def get_move(self, game_state: GameState) -> str:
        raise NotImplementedError


class RandomPlayer(Player):
    """
    Example: a random AI that picks a valid direction that avoids walls and self-collisions.
    """
    def get_move(self, game_state: GameState) -> str:
        snake_positions = game_state.snake_positions[self.snake_id]
        head_x, head_y = snake_positions[0]
        
        # Calculate all possible next positions
        possible_moves = {
            UP:    (head_x, head_y + 1),  # Up => y + 1
            DOWN:  (head_x, head_y - 1),  # Down => y - 1
            LEFT:  (head_x - 1, head_y),
            RIGHT: (head_x + 1, head_y)
        }
        
        # Filter out moves that:
        # 1. Hit walls
        # 2. Hit own body (except tail, which will move)
        valid_moves = []
        for move, (new_x, new_y) in possible_moves.items():
            # Check wall collisions
            if (new_x < 0 or new_x >= game_state.width or 
                new_y < 0 or new_y >= game_state.height):
                continue
                
            # Check self collisions (excluding tail which will move)
            if (new_x, new_y) in snake_positions[:-1]:
                continue
                
            valid_moves.append(move)
        
        # If no valid moves, just return a random move (we'll die anyway)
        if not valid_moves:
            return random.choice(list(VALID_MOVES))
            
        return random.choice(valid_moves)


class LLMPlayer(Player):
    """
    LLM-based player that delegates the API call details to the provider abstraction.
    """
    def __init__(self, snake_id: str, model: str = "gpt-4o-mini"):
        super().__init__(snake_id)
        self.model = model
        self.move_history = []
        # Instantiate the correct provider based on the model name.
        self.provider = create_llm_provider(model)

    def get_direction_from_response(self, response: str) -> Optional[str]:
        # Convert response to uppercase for case-insensitive comparison.
        response = response.upper()
        # Starting from the end, find the last occurrence of any valid move.
        for i in range(len(response) - 1, -1, -1):
            for move in VALID_MOVES:
                if response[i:].startswith(move):
                    return move.upper()
        return None

    def get_move(self, game_state: GameState) -> str:
        """
        Construct the prompt, call the generic provider, and then parse the response.
        """
        prompt = self._construct_prompt(game_state)

        # Use the abstracted provider to get the response.
        response_text = self.provider.get_response(self.model, prompt)
        direction = self.get_direction_from_response(response_text)

        if direction is None:
            direction = random.choice(list(VALID_MOVES))

        move_data = {
            "direction": direction,
            "rationale": response_text
        }

        self.move_history.append({self.snake_id: move_data})
        return move_data

    def _construct_prompt(self, game_state: GameState) -> str:
        # Summarize the multiple apples
        apples_str = ", ".join(str(a) for a in game_state.apples)
        prompt = (
            f"You are controlling a snake in a multi-apple Snake game. "
            f"The board size is {game_state.width}x{game_state.height}. Normal X,Y coordinates are used. Coordinates range from (0,0) at bottom left to ({game_state.width-1},{game_state.height-1}) at top right.\n"
            f"Apples at: {apples_str}\n"
            f"Your snake ID: {self.snake_id} which is currently positioned at {game_state.snake_positions[self.snake_id][0]}\n\n"
            f"Enemy snakes positions:\n" + 
            "\n".join([f"* Snake #{sid} is at position {pos[0]} with body at {pos[1:]}" for sid, pos in game_state.snake_positions.items() if sid != self.snake_id]) + "\n\n"
            f"Board state:\n"
            f"{game_state.print_board()}\n\n"
            f"--Your last move information:--\n\n"
            f"Direction: {self.move_history[-1][self.snake_id]['direction'] if self.move_history else 'None'}\n"
            f"Rationale: {self.move_history[-1][self.snake_id]['rationale'] if self.move_history else 'None'}\n\n"
            f"--End of your last move information.--\n\n"
            "Rules:\n"
            "1) If you move onto an apple, you grow and gain 1 point.\n"
            "2) If you run into a wall (outside the range of the listed coordinates), another snake, or yourself (like go backwards), you die.\n"
            "3) The goal is to have the most points by the end.\n\n"
            "Decreasing your x coordinate is to the left, increasing your x coordinate is to the right.\n"
            "Decreasing your y coordinate is down, increasing your y coordinate is up.\n"
            "You may think out loud first then respond with the direction.\n"
            "You may also state a strategy you want to tell yourself next turn.\n"
            "End your response with your decided next move: UP, DOWN, LEFT, or RIGHT.\n"
        )
        print(f"----------Prompt:\n\n {prompt}\n\n------------")
        return prompt

class SnakeGame:
    """
    Manages:
      - Board (width, height)
      - Snakes
      - Players
      - Multiple apples
      - Scores
      - Rounds
      - History for replay
    """
    def __init__(self, width: int, height: int, max_rounds: int = 20, num_apples: int = 3, game_id: str = None):
        self.width = width
        self.height = height
        self.snakes: Dict[str, Snake] = {}
        self.players: Dict[str, Player] = {}
        self.scores: Dict[str, int] = {}
        self.round_number = 0
        self.max_rounds = max_rounds
        self.game_over = False
        self.start_time = time.time()
        self.game_result = None 

        if game_id is None:
            self.game_id = str(uuid.uuid4())
        else:
            self.game_id = game_id
        print(f"Game ID: {self.game_id}")

        # Store how many apples we want to keep on the board at all times
        self.num_apples = num_apples
        
        # We store multiple apples as a set of (x, y) or a list.
        # Here, let's keep them as a list to preserve GameState JSON-friendliness.
        self.apples: List[Tuple[int,int]] = []

        # For replay or for the LLM context
        self.move_history: List[Dict[str, str]] = []
        self.history: List[GameState] = []

        # Place initial apples
        for _ in range(self.num_apples):
            cell = self._random_free_cell()
            self.apples.append(cell)

    def add_snake(self, snake_id: str, player: Player):
        if snake_id in self.snakes:
            raise ValueError(f"Snake with id {snake_id} already exists.")
        
        positions = self._random_free_cell()

        self.snakes[snake_id] = Snake([positions])
        self.players[snake_id] = player
        self.scores[snake_id] = 0
        print(f"Added snake '{snake_id}' ({player.model if hasattr(player, 'model') else player.__class__.__name__}) at {positions}.")

    def set_apples(self, apple_positions: List[Tuple[int,int]]):
        """
        Initialize the board with multiple apples at specified positions.
        If you want random generation, you can do that here.
        """
        for (ax, ay) in apple_positions:
            if not (0 <= ax < self.width and 0 <= ay < self.height):
                raise ValueError(f"Apple out of bounds at {(ax, ay)}.")
        self.apples = list(apple_positions)
        print(f"Set {len(self.apples)} apples on the board: {self.apples}")

    def _random_free_cell(self) -> Tuple[int,int]:
        """
        Return a random cell (x, y) not occupied by any snake or apple.
        We'll do a simple loop to find one. 
        """
        while True:
            x = random.randint(0, self.width - 1)
            y = random.randint(0, self.height - 1)
            # Check if occupied by a snake
            occupied_by_snake = any((x, y) in snake.positions for snake in self.snakes.values())
            # Check if there's already an apple here
            occupied_by_apple = (x, y) in self.apples

            if not occupied_by_snake and not occupied_by_apple:
                return (x, y)
    
    def get_current_state(self) -> GameState:
        """
        Return a snapshot of the current board as a GameState.
        """
        snake_positions = {}
        alive_dict = {}
        for sid, snake in self.snakes.items():
            snake_positions[sid] = list(snake.positions)
            alive_dict[sid] = snake.alive

        return GameState(
            round_number=self.round_number,
            snake_positions=snake_positions,
            alive=alive_dict,
            scores=self.scores.copy(),
            width=self.width,
            height=self.height,
            apples=self.apples.copy(),
            move_history=list(self.move_history)
        )

    def run_round(self):
        """
        Execute one round:
          1) If game is over, do nothing
          2) Ask each alive snake for their move in parallel
          3) Apply moves simultaneously
          4) Handle apple-eating (grow + score)
          5) Check collisions
          6) Possibly end game if round limit reached or 1 snake left, etc.
        """
        if self.game_over:
            print("Game is already over. No more rounds.")
            return
        
        self.print_board()

        # Capture the current state once so that all players work off the same snapshot.
        current_state = self.get_current_state()

        # 1) Gather moves concurrently using a ThreadPoolExecutor.
        round_moves = {}
        import concurrent.futures  # Ensure this is imported if not already present at the module level.
        with concurrent.futures.ThreadPoolExecutor() as executor:
            # Schedule get_move calls for all alive snakes
            future_to_snake = {
                executor.submit(self.players[snake_id].get_move, current_state): snake_id
                for snake_id, snake in self.snakes.items() if snake.alive
            }

            # Collect results as they are completed.
            for future in concurrent.futures.as_completed(future_to_snake):
                snake_id = future_to_snake[future]
                try:
                    result = future.result()  # Expect a dict with "direction" and "rationale"
                    move = result["direction"]
                    rationale = result["rationale"]
                    round_moves[snake_id] = {"move": move, "rationale": rationale}
                    print(f"Player {snake_id} chose move: {move}")
                except Exception as e:
                    # On error, assign a random move (fallback)
                    move = random.choice(list(VALID_MOVES))
                    round_moves[snake_id] = {"move": move, "rationale": f"Error: {e}"}
                    print(f"Error for snake {snake_id}: {str(e)}")

        # For any snakes that are not alive, assign their move as None.
        for snake_id, snake in self.snakes.items():
            if not snake.alive:
                round_moves[snake_id] = None

        # Store the moves of this round.
        self.move_history.append(round_moves)

        # 2) Compute new head positions.
        new_heads: Dict[str, Optional[Tuple[int,int]]] = {}
        for sid, move_data in round_moves.items():
            snake = self.snakes[sid]
            if not snake.alive or move_data is None:
                new_heads[sid] = None
                continue
            
            move = move_data["move"]
            hx, hy = snake.head
            if move == UP:
                hy += 1
            elif move == DOWN:
                hy -= 1
            elif move == LEFT:
                hx -= 1
            elif move == RIGHT:
                hx += 1
            new_heads[sid] = (hx, hy)

        # 3) Handle collisions.
        cell_counts: Dict[Tuple[int,int], List[str]] = {}
        for sid, head_pos in new_heads.items():
            if head_pos is not None:
                cell_counts.setdefault(head_pos, []).append(sid)

        for sid, head_pos in new_heads.items():
            snake = self.snakes[sid]
            if not snake.alive or head_pos is None:
                continue

            x, y = head_pos
            # Wall collision.
            if x < 0 or x >= self.width or y < 0 or y >= self.height:
                snake.alive = False
                snake.death_reason = 'wall'
                snake.death_round = self.round_number
                continue

            # Collision with any snake's body (including tails).
            for _, other_snake in self.snakes.items():
                if head_pos in other_snake.positions:
                    snake.alive = False
                    snake.death_reason = 'body_collision'
                    snake.death_round = self.round_number
                    break

        for sid, head_pos in new_heads.items():
            snake = self.snakes[sid]
            if not snake.alive or head_pos is None:
                continue

            if len(cell_counts[head_pos]) > 1:
                snake.alive = False
                snake.death_reason = 'head_collision'
                snake.death_round = self.round_number

        # Early game outcome if one or more snakes die this round.
        snakes_died_this_round = [
            sid for sid, s in self.snakes.items()
            if not s.alive and s.death_round == self.round_number
        ]

        if len(snakes_died_this_round) > 0:
            if len(snakes_died_this_round) == 1 and len(self.snakes) == 2:
                surviving_snakes = [sid for sid, s in self.snakes.items() if s.alive]
                self.game_over = True
                self.game_result = {sid: "lost" for sid in self.snakes}
                for sid in surviving_snakes:
                    self.game_result[sid] = "won"
                print(f"Game Over: Snake(s) {snakes_died_this_round} died. "
                      f"Survivor(s) {surviving_snakes} win(s).")
                self.record_history()
                return

            elif len(snakes_died_this_round) >= 2 and len(self.snakes) == 2:
                self.game_over = True
                self.game_result = {sid: "tied" for sid in self.snakes}
                print("Game Over: Both snakes died this round. It's a tie!")
                self.record_history()
                return

        # 4) Move snakes and handle apple eating/growth.
        for sid, snake in self.snakes.items():
            if snake.alive and new_heads[sid] is not None:
                head_pos = new_heads[sid]
                if head_pos in self.apples:
                    snake.positions.appendleft(head_pos)
                    self.scores[sid] += 1
                    self.apples.remove(head_pos)
                    new_apple = self._random_free_cell()
                    self.apples.append(new_apple)
                else:
                    snake.positions.appendleft(head_pos)
                    snake.positions.pop()

        # 5) End the round, record state, and check round limits.
        self.round_number += 1
        self.record_history()

        if self.round_number >= self.max_rounds:
            self.end_game("Reached max rounds.")
        else:
            alive_snakes = [s_id for s_id in self.snakes if self.snakes[s_id].alive]
            if len(alive_snakes) <= 1:
                self.end_game("All but one snake are dead.")

        print(f"Finished round {self.round_number}. Alive: {[sid for sid in self.snakes if self.snakes[sid].alive]}, Scores: {self.scores}")
        time.sleep(.3)

    def serialize_history(self, history):
        """
        Convert the list of GameState objects to a JSON-serializable list of dicts.
        """
        output = []
        for state in history:
            # Build a dictionary representation
            state_dict = {
                "round_number": state.round_number,
                "snake_positions": {
                    sid: positions  # positions is already a list of (x, y)
                    for sid, positions in state.snake_positions.items()
                },
                "alive": state.alive,         # dict of snake_id -> bool
                "scores": state.scores,       # dict of snake_id -> int
                "width": state.width,
                "height": state.height,
                "apples": state.apples,       # list of (x, y)
                "move_history": state.move_history
            }
            # Note: If any data is in tuples, it's okay because JSON
            # can store them as lists. But Python's json library will 
            # automatically convert (x, y) to [x, y].
            output.append(state_dict)
        return output
    
    def clean_model_name(self, model_name: str) -> str:
        # Check if the model name contains a provider prefix (e.g., "mistral/")
        if '/' in model_name:
            # Return everything after the last '/'
            return model_name.split('/')[-1]
        # If no provider prefix, return the original name
        return model_name


    def save_history_to_json(self, filename=None):
        if filename is None:
            filename = f"snake_game_{self.game_id}.json"

        model_names = {
            sid: self.clean_model_name(player.model if hasattr(player, "model") else player.__class__.__name__)
            for sid, player in self.players.items()
        }
        
        # Build metadata for the game
        metadata = {
            "game_id": self.game_id,
            "start_time": datetime.fromtimestamp(self.start_time).isoformat(),
            "end_time": datetime.fromtimestamp(time.time()).isoformat(),
            "models": model_names,
            "game_result": self.game_result,
            "final_scores": self.scores,
            "death_info": {
                sid: {
                    "reason": snake.death_reason,
                    "round": snake.death_round
            }
            for sid, snake in self.snakes.items()
            if not snake.alive  # you could record info for dead snakes only
        },
            "max_rounds": self.max_rounds,
            "actual_rounds": self.round_number
        }
        
        data = {
            "metadata": metadata,
            "rounds": self.serialize_history(self.history)
        }
        
        # Ensure the output directory exists
        os.makedirs('completed_games', exist_ok=True)
        
        with open(f'completed_games/{filename}', "w") as f:
            json.dump(data, f, indent=2)
    
    def print_board(self):
        """
        Prints a visual representation of the current board state.
        """
        print("\n" + self.get_current_state().print_board() + "\n")

    def end_game(self, reason: str):
        self.game_over = True
        print(f"Game Over: {reason}")
        # Decide winner by highest score
        top_score = max(self.scores.values()) if self.scores else 0
        winners = [sid for sid, sc in self.scores.items() if sc == top_score]
        
        # Record the game result per snake
        self.game_result = {}
        for sid in self.scores:
            if sid in winners:
                self.game_result[sid] = "tied" if len(winners) > 1 else "won"
            else:
                self.game_result[sid] = "lost"
        
        if len(winners) == 1:
            print(f"The winner is {winners[0]} with score {top_score}.")
        else:
            print(f"Tie! Winners: {winners} with score {top_score}.")

    def record_history(self):
        state = self.get_current_state()
        self.history.append(state)


# -------------------------------
# Example Usage
# -------------------------------
def main():
    # Create a game with a 5x5 board and 100 rounds (you can adjust these as needed)
    game = SnakeGame(width=30, height=30, max_rounds=200, num_apples=120)

    # Load models.txt
    with open('models.txt', 'r') as file:
        model_names = [line.strip() for line in file.readlines()]

    for model_name in model_names:
        game.add_snake(
            snake_id=model_name,
            player=LLMPlayer(model_name, model=model_name)
        )

    # Record initial state and run the game
    game.record_history()
    while not game.game_over:
        game.run_round()

    print("\nFinal Scores:", game.scores)
    print("Game history (round by round):")
    for gs in game.history:
        print(gs)

    game.save_history_to_json()

if __name__ == "__main__":
    main()