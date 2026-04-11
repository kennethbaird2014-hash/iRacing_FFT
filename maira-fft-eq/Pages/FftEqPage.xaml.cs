
using System.ComponentModel;
using System.Numerics;
using System.Runtime.CompilerServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Shapes;
using System.Windows.Threading;

using UserControl = System.Windows.Controls.UserControl;

using MarvinsAIRARefactored.Components;

namespace MarvinsAIRARefactored.Pages;

// ── Per-band view-model ────────────────────────────────────────────────────

public class EqBandViewModel : INotifyPropertyChanged
{
	private bool       _enabled = true;
	private FilterType _type    = FilterType.Peak;
	private float      _freq    = 10f;
	private float      _gainDb  = 0f;
	private float      _q       = 1.4f;

	public bool Enabled
	{
		get => _enabled;
		set { if ( _enabled != value ) { _enabled = value; OnPropertyChanged(); Changed?.Invoke(); } }
	}

	public FilterType Type
	{
		get => _type;
		set { if ( _type != value ) { _type = value; OnPropertyChanged(); Changed?.Invoke(); } }
	}

	public float Freq
	{
		get => _freq;
		set
		{
			value = Math.Clamp( value, 0.3f, 220f );
			if ( Math.Abs( _freq - value ) > 0.001f ) { _freq = value; OnPropertyChanged(); OnPropertyChanged( nameof( FreqDisplay ) ); Changed?.Invoke(); }
		}
	}

	public float GainDb
	{
		get => _gainDb;
		set
		{
			value = Math.Clamp( value, -24f, 24f );
			if ( Math.Abs( _gainDb - value ) > 0.001f ) { _gainDb = value; OnPropertyChanged(); OnPropertyChanged( nameof( GainDisplay ) ); Changed?.Invoke(); }
		}
	}

	public float Q
	{
		get => _q;
		set
		{
			value = Math.Clamp( value, 0.1f, 20f );
			if ( Math.Abs( _q - value ) > 0.001f ) { _q = value; OnPropertyChanged(); OnPropertyChanged( nameof( QDisplay ) ); Changed?.Invoke(); }
		}
	}

	public string FreqDisplay => $"{_freq:F1} Hz";
	public string GainDisplay => _gainDb >= 0 ? $"+{_gainDb:F1}" : $"{_gainDb:F1}";
	public string QDisplay    => $"{_q:F2}";

	public event Action? Changed;

	public event PropertyChangedEventHandler? PropertyChanged;
	private void OnPropertyChanged( [CallerMemberName] string? name = null ) =>
		PropertyChanged?.Invoke( this, new PropertyChangedEventArgs( name ) );

	public EqBand ToEqBand() => new() { Freq = _freq, GainDb = _gainDb, Q = _q, Type = _type, Enabled = _enabled };

	public static EqBandViewModel FromEqBand( EqBand b ) =>
		new() { Enabled = b.Enabled, Type = b.Type, Freq = b.Freq, GainDb = b.GainDb, Q = b.Q };
}

// ── Page code-behind ───────────────────────────────────────────────────────

public partial class FftEqPage : UserControl
{
	// Display constants
	private const double DbMin      = -50.0;
	private const double DbMax      = +25.0;
	private const double FreqMin    = 1.0;
	private const double FreqMax    = 240.0;
	private const double CanvasPad  = 2.0;

	private readonly List<EqBandViewModel> _bands = [];

	private readonly DispatcherTimer _displayTimer = new() { Interval = TimeSpan.FromMilliseconds( 34 ) };

	// Spectrum data buffers (UI-thread only, filled by GetSpectrum each tick)
	private double[]? _rawDb;
	private double[]? _fxDb;

	// Persistent Canvas polylines so we don't recreate them every frame
	private Polyline? _rawLine;
	private Polyline? _fxLine;
	private Polyline? _eqCurveLine;
	private bool _canvasReady = false;

	// Band row colors (cycles for visual distinction)
	private static readonly Brush[] BandColors =
	[
		new SolidColorBrush( Color.FromRgb( 255, 180,  60 ) ),
		new SolidColorBrush( Color.FromRgb(  60, 200, 255 ) ),
		new SolidColorBrush( Color.FromRgb( 200,  80, 255 ) ),
		new SolidColorBrush( Color.FromRgb( 255,  80,  80 ) ),
		new SolidColorBrush( Color.FromRgb( 100, 255, 100 ) ),
		new SolidColorBrush( Color.FromRgb( 255, 160, 200 ) ),
	];

	public FftEqPage()
	{
		InitializeComponent();

		Loaded   += OnLoaded;
		Unloaded += OnUnloaded;
	}

	private void OnLoaded( object sender, RoutedEventArgs e )
	{
		LoadBandsFromApp();
		BuildSpectrumCanvas();

		_displayTimer.Tick += OnDisplayTick;
		_displayTimer.Start();
	}

	private void OnUnloaded( object sender, RoutedEventArgs e )
	{
		_displayTimer.Stop();
		_displayTimer.Tick -= OnDisplayTick;
	}

	// ── Band management ────────────────────────────────────────────────────

	private void LoadBandsFromApp()
	{
		var app = App.Instance!;

		_bands.Clear();

		foreach ( var b in app.FftEq.LoadBands() )
		{
			var vm = EqBandViewModel.FromEqBand( b );
			vm.Changed += OnAnyBandChanged;
			_bands.Add( vm );
		}

		RebuildBandRows();
	}

	private void RebuildBandRows()
	{
		BandRows_StackPanel.Children.Clear();

		for ( int i = 0; i < _bands.Count; i++ )
		{
			BandRows_StackPanel.Children.Add( BuildBandRow( i ) );
		}
	}

	private Grid BuildBandRow( int index )
	{
		var vm    = _bands[ index ];
		var color = BandColors[ index % BandColors.Length ];

		var row = new Grid { Margin = new Thickness( 0, 0, 0, 6 ) };

		row.ColumnDefinitions.Add( new ColumnDefinition { Width = new GridLength( 30 ) } );  // enabled
		row.ColumnDefinitions.Add( new ColumnDefinition { Width = new GridLength( 100 ) } ); // type
		row.ColumnDefinitions.Add( new ColumnDefinition { Width = new GridLength( 10 ) } );  // gap
		row.ColumnDefinitions.Add( new ColumnDefinition { Width = new GridLength( 1, GridUnitType.Star ) } ); // freq
		row.ColumnDefinitions.Add( new ColumnDefinition { Width = new GridLength( 10 ) } );  // gap
		row.ColumnDefinitions.Add( new ColumnDefinition { Width = new GridLength( 1, GridUnitType.Star ) } ); // gain
		row.ColumnDefinitions.Add( new ColumnDefinition { Width = new GridLength( 10 ) } );  // gap
		row.ColumnDefinitions.Add( new ColumnDefinition { Width = new GridLength( 1, GridUnitType.Star ) } ); // Q
		row.ColumnDefinitions.Add( new ColumnDefinition { Width = new GridLength( 10 ) } );  // gap
		row.ColumnDefinitions.Add( new ColumnDefinition { Width = new GridLength( 30 ) } );  // remove

		// Enabled checkbox
		var chk = new CheckBox
		{
			IsChecked       = vm.Enabled,
			VerticalAlignment = VerticalAlignment.Center,
			Foreground        = color
		};
		chk.Checked   += ( _, _ ) => { vm.Enabled = true;  };
		chk.Unchecked += ( _, _ ) => { vm.Enabled = false; };
		Grid.SetColumn( chk, 0 );
		row.Children.Add( chk );

		// Filter type combobox
		var typeBox = new ComboBox
		{
			ItemsSource   = Enum.GetValues<FilterType>(),
			SelectedItem  = vm.Type,
			VerticalContentAlignment = VerticalAlignment.Center
		};
		typeBox.SelectionChanged += ( _, _ ) =>
		{
			if ( typeBox.SelectedItem is FilterType ft )
				vm.Type = ft;
		};
		Grid.SetColumn( typeBox, 1 );
		row.Children.Add( typeBox );

		// Freq slider + label
		var freqPanel = BuildSliderPanel( vm.FreqDisplay, color,
			sliderValue: FreqToSlider( vm.Freq ),
			min: 0, max: 100,
			onChanged: v => { vm.Freq = SliderToFreq( v ); } );
		Grid.SetColumn( freqPanel, 3 );
		row.Children.Add( freqPanel );

		// Gain slider + label
		var gainPanel = BuildSliderPanel( vm.GainDisplay, color,
			sliderValue: vm.GainDb,
			min: -24, max: 24,
			onChanged: v => { vm.GainDb = (float) v; } );
		Grid.SetColumn( gainPanel, 5 );
		row.Children.Add( gainPanel );

		// Q slider + label
		var qPanel = BuildSliderPanel( vm.QDisplay, color,
			sliderValue: vm.Q,
			min: 0.1, max: 10,
			onChanged: v => { vm.Q = (float) v; } );
		Grid.SetColumn( qPanel, 7 );
		row.Children.Add( qPanel );

		// Remove button
		var removeBtn = new Button
		{
			Content            = "×",
			Width              = 24,
			Height             = 24,
			FontSize           = 14,
			Padding            = new Thickness( 0 ),
			VerticalAlignment  = VerticalAlignment.Center,
			HorizontalAlignment = HorizontalAlignment.Center
		};
		int capturedIndex = index;
		removeBtn.Click += ( _, _ ) => RemoveBand( capturedIndex );
		Grid.SetColumn( removeBtn, 9 );
		row.Children.Add( removeBtn );

		return row;
	}

	private static Grid BuildSliderPanel( string labelText, Brush color,
		double sliderValue, double min, double max, Action<double> onChanged )
	{
		var panel = new Grid();
		panel.ColumnDefinitions.Add( new ColumnDefinition { Width = new GridLength( 1, GridUnitType.Star ) } );
		panel.ColumnDefinitions.Add( new ColumnDefinition { Width = new GridLength( 54 ) } );

		var slider = new Slider
		{
			Minimum           = min,
			Maximum           = max,
			Value             = sliderValue,
			VerticalAlignment = VerticalAlignment.Center
		};

		var label = new TextBlock
		{
			Text              = labelText,
			Foreground        = color,
			FontSize          = 11,
			VerticalAlignment = VerticalAlignment.Center,
			TextAlignment     = TextAlignment.Right,
			Margin            = new Thickness( 4, 0, 0, 0 )
		};

		slider.ValueChanged += ( _, e ) => onChanged( e.NewValue );

		Grid.SetColumn( slider, 0 );
		Grid.SetColumn( label, 1 );

		panel.Children.Add( slider );
		panel.Children.Add( label );

		return panel;
	}

	// Log-scale mapping for frequency slider  (slider 0→100 maps to 0.3→220 Hz)
	private static double FreqToSlider( float freq ) =>
		Math.Log10( Math.Max( freq, 0.3f ) / 0.3 ) / Math.Log10( 220.0 / 0.3 ) * 100.0;

	private static float SliderToFreq( double sliderVal ) =>
		(float) ( 0.3 * Math.Pow( 220.0 / 0.3, sliderVal / 100.0 ) );

	private void OnAnyBandChanged()
	{
		// Persist + push to processor (called on UI thread from slider ValueChanged)
		PushBandsToApp();
		RefreshBandRowLabels();
		RefreshEqCurve();
	}

	private void PushBandsToApp()
	{
		var app = App.Instance!;
		app.FftEq.SaveBands( _bands.Select( vm => vm.ToEqBand() ).ToList() );
	}

	private void RefreshBandRowLabels()
	{
		// Walk the StackPanel rows and update the TextBlock labels inside slider panels
		for ( int rowIdx = 0; rowIdx < BandRows_StackPanel.Children.Count && rowIdx < _bands.Count; rowIdx++ )
		{
			var vm      = _bands[ rowIdx ];
			var rowGrid = (Grid) BandRows_StackPanel.Children[ rowIdx ];

			for ( int c = 0; c < rowGrid.Children.Count; c++ )
			{
				if ( rowGrid.Children[ c ] is not Grid sliderPanel ) continue;

				int col = Grid.GetColumn( sliderPanel );

				for ( int sc = 0; sc < sliderPanel.Children.Count; sc++ )
				{
					if ( sliderPanel.Children[ sc ] is not TextBlock lbl ) continue;

					lbl.Text = col switch
					{
						3 => vm.FreqDisplay,
						5 => vm.GainDisplay,
						7 => vm.QDisplay,
						_ => lbl.Text
					};
				}
			}
		}
	}

	// ── Toolbar handlers ───────────────────────────────────────────────────

	private void AddBand_Click( object sender, RoutedEventArgs e )
	{
		var vm = new EqBandViewModel { Freq = 20f, GainDb = 0f, Q = 1.4f, Type = FilterType.Peak };
		vm.Changed += OnAnyBandChanged;
		_bands.Add( vm );
		RebuildBandRows();
		PushBandsToApp();
		RefreshEqCurve();
	}

	private void RemoveBand( int index )
	{
		if ( index < 0 || index >= _bands.Count ) return;

		_bands[ index ].Changed -= OnAnyBandChanged;
		_bands.RemoveAt( index );
		RebuildBandRows();
		PushBandsToApp();
		RefreshEqCurve();
	}

	private void ResetBands_Click( object sender, RoutedEventArgs e )
	{
		foreach ( var vm in _bands )
			vm.Changed -= OnAnyBandChanged;

		_bands.Clear();

		// Default set
		foreach ( var b in new EqBand[]
		{
			new() { Freq =  5f, GainDb = 0f, Q = 1.4f, Type = FilterType.LowShelf  },
			new() { Freq = 20f, GainDb = 0f, Q = 1.4f, Type = FilterType.Peak      },
			new() { Freq = 80f, GainDb = 0f, Q = 1.4f, Type = FilterType.HighShelf },
		} )
		{
			var vm = EqBandViewModel.FromEqBand( b );
			vm.Changed += OnAnyBandChanged;
			_bands.Add( vm );
		}

		RebuildBandRows();
		PushBandsToApp();
		RefreshEqCurve();
	}

	// ── Spectrum canvas setup ──────────────────────────────────────────────

	private void BuildSpectrumCanvas()
	{
		SpectrumCanvas.Children.Clear();
		FreqLabelCanvas.Children.Clear();
		_canvasReady = false;

		// Ensure canvas has been measured at least once
		SpectrumCanvas.UpdateLayout();

		double W = SpectrumCanvas.ActualWidth;
		double H = SpectrumCanvas.ActualHeight;

		if ( W < 10 || H < 10 )
		{
			// Defer until the canvas has a real size
			SpectrumCanvas.SizeChanged += ( _, _ ) =>
			{
				if ( !_canvasReady )
					BuildSpectrumCanvas();
			};
			return;
		}

		// Background
		var bg = new Rectangle
		{
			Width            = W,
			Height           = H,
			Fill             = new SolidColorBrush( Color.FromRgb( 0x11, 0x11, 0x11 ) )
		};
		Canvas.SetLeft( bg, 0 );
		Canvas.SetTop(  bg, 0 );
		SpectrumCanvas.Children.Add( bg );

		// Horizontal dB grid lines
		var gridBrush = new SolidColorBrush( Color.FromArgb( 60, 255, 255, 255 ) );
		var zeroBrush = new SolidColorBrush( Color.FromArgb( 100, 255, 255, 255 ) );

		foreach ( double db in new[] { -40.0, -30.0, -20.0, -10.0, 0.0, 10.0, 20.0 } )
		{
			if ( db < DbMin || db > DbMax ) continue;
			double y = DbToY( db, H );

			var line = new Line
			{
				X1             = CanvasPad,
				X2             = W - CanvasPad,
				Y1             = y,
				Y2             = y,
				Stroke         = db == 0 ? zeroBrush : gridBrush,
				StrokeThickness = db == 0 ? 1.0 : 0.5
			};
			SpectrumCanvas.Children.Add( line );

			// dB label
			var lbl = new TextBlock
			{
				Text       = $"{db:+0;-0;0} dB",
				Foreground = new SolidColorBrush( Color.FromArgb( 120, 200, 200, 200 ) ),
				FontSize   = 9
			};
			Canvas.SetRight( lbl, CanvasPad + 2 );
			Canvas.SetTop(   lbl, y - 8 );
			FreqLabelCanvas.Children.Add( lbl );
		}

		// Vertical frequency grid lines
		double[] freqMarkers = [ 1, 2, 5, 10, 20, 50, 100, 200 ];

		foreach ( double f in freqMarkers )
		{
			if ( f < FreqMin || f > FreqMax ) continue;
			double x = FreqToX( f, W );

			var line = new Line
			{
				X1              = x,
				X2              = x,
				Y1              = CanvasPad,
				Y2              = H - CanvasPad,
				Stroke          = gridBrush,
				StrokeThickness = 0.5
			};
			SpectrumCanvas.Children.Add( line );

			var lbl = new TextBlock
			{
				Text       = f >= 1000 ? $"{f / 1000:F0}k" : $"{f:F0} Hz",
				Foreground = new SolidColorBrush( Color.FromArgb( 120, 200, 200, 200 ) ),
				FontSize   = 9
			};
			Canvas.SetLeft( lbl, x + 2 );
			Canvas.SetTop(  lbl, H - 14 );
			FreqLabelCanvas.Children.Add( lbl );
		}

		// Raw spectrum polyline (green, semi-transparent)
		_rawLine = new Polyline
		{
			Stroke          = new SolidColorBrush( Color.FromArgb( 160, 68, 187, 102 ) ),
			StrokeThickness = 1.5,
			Points          = []
		};
		SpectrumCanvas.Children.Add( _rawLine );

		// Post-EQ spectrum polyline (cyan)
		_fxLine = new Polyline
		{
			Stroke          = new SolidColorBrush( Color.FromArgb( 220, 0, 204, 204 ) ),
			StrokeThickness = 1.5,
			Points          = []
		};
		SpectrumCanvas.Children.Add( _fxLine );

		// Total EQ response polyline (white, dashed)
		_eqCurveLine = new Polyline
		{
			Stroke              = Brushes.White,
			StrokeThickness     = 1.5,
			StrokeDashArray     = [ 4, 3 ],
			Points              = []
		};
		SpectrumCanvas.Children.Add( _eqCurveLine );

		// Allocate spectrum buffers
		var app = App.Instance;
		if ( app != null )
		{
			int sz  = app.FftEq.Processor.SpectrumSize;
			_rawDb  = new double[ sz ];
			_fxDb   = new double[ sz ];
		}

		_canvasReady = true;

		// Draw initial EQ curve
		RefreshEqCurve();
	}

	// ── Display tick (~30 Hz) ──────────────────────────────────────────────

	private void OnDisplayTick( object? sender, EventArgs e )
	{
		if ( !_canvasReady ) return;

		var app = App.Instance;
		if ( app == null ) return;

		double W = SpectrumCanvas.ActualWidth;
		double H = SpectrumCanvas.ActualHeight;
		if ( W < 10 || H < 10 ) return;

		// Pull latest spectrum from processor
		app.FftEq.Processor.GetSpectrum( _rawDb!, _fxDb! );

		UpdateSpectrumPolyline( _rawLine!,     _rawDb!, app.FftEq.Processor.FreqBins, W, H );
		UpdateSpectrumPolyline( _fxLine!,      _fxDb!,  app.FftEq.Processor.FreqBins, W, H );
	}

	private void UpdateSpectrumPolyline( Polyline poly, double[] dbData,
		double[] freqBins, double W, double H )
	{
		var pts = poly.Points;
		pts.Clear();

		for ( int i = 1; i < dbData.Length; i++ ) // skip DC bin (i=0)
		{
			double f  = freqBins[ i ];
			if ( f < FreqMin || f > FreqMax ) continue;

			double db = Math.Max( dbData[ i ], DbMin );
			double x  = FreqToX( f,  W );
			double y  = DbToY(   db, H );

			pts.Add( new Point( x, y ) );
		}
	}

	// ── EQ response curve (analytical, from SOS) ──────────────────────────

	private void RefreshEqCurve()
	{
		if ( !_canvasReady || _eqCurveLine == null ) return;

		double W = SpectrumCanvas.ActualWidth;
		double H = SpectrumCanvas.ActualHeight;
		if ( W < 10 || H < 10 ) return;

		var pts        = _eqCurveLine.Points;
		pts.Clear();

		var bands = _bands.Where( b => b.Enabled ).Select( b => b.ToEqBand() ).ToList();

		// Evaluate at 200 log-spaced points across display range
		const int N = 200;

		for ( int i = 0; i < N; i++ )
		{
			double t = (double) i / ( N - 1 );
			double f = FreqMin * Math.Pow( FreqMax / FreqMin, t );

			var H_total = new Complex( 1, 0 );

			foreach ( var band in bands )
				H_total *= band.GetResponse( f, FftEqProcessor.SampleRate );

			double db = Math.Max( 20.0 * Math.Log10( H_total.Magnitude + 1e-12 ), DbMin );

			pts.Add( new Point( FreqToX( f, W ), DbToY( db, H ) ) );
		}
	}

	// ── Coordinate helpers ─────────────────────────────────────────────────

	private static double FreqToX( double freq, double canvasWidth )
	{
		double logMin = Math.Log10( FreqMin );
		double logMax = Math.Log10( FreqMax );
		double logF   = Math.Log10( Math.Max( freq, FreqMin ) );
		return CanvasPad + ( logF - logMin ) / ( logMax - logMin ) * ( canvasWidth - 2 * CanvasPad );
	}

	private static double DbToY( double db, double canvasHeight )
	{
		double t = ( db - DbMax ) / ( DbMin - DbMax );
		return CanvasPad + t * ( canvasHeight - 2 * CanvasPad );
	}
}
