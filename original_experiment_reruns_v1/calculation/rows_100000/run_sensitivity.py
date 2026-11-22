import benchmark_computational_channel


if __name__=='__main__':

    benchmark_computational_channel.RESULT_ROOT=benchmark_computational_channel.RESULT_ROOT/'sensitivity'
    benchmark_computational_channel.REPEATS=3
    benchmark_computational_channel.DELAY_GRID_MS=[(0.2,0),(0,0.2)]
    benchmark_computational_channel.main()
