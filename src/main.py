# This is the smallest possible starter program for a new SPIKE project.
# It gives children an immediate success moment: as soon as the program runs,
# the Hub screen says "HI".
#
# After that works, you can replace this file with your own ideas:
# motors, sensors, animations, games, or robots.

from hub import light_matrix


# Show a short greeting on the Hub screen.
light_matrix.write("HI")
